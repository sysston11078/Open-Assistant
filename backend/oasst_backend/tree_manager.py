import json
import random
import sys
from datetime import datetime, timedelta
from enum import Enum
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import numpy as np
import pydantic
from fastapi.encoders import jsonable_encoder
from loguru import logger
from oasst_backend.api.v1.utils import (
    prepare_conversation,
    prepare_conversation_message,
    prepare_conversation_message_list,
)
from oasst_backend.config import TreeManagerConfiguration, settings
from oasst_backend.models import Message, MessageReaction, MessageTreeState, Task, TextLabels, User, message_tree_state
from oasst_backend.prompt_repository import PromptRepository
from oasst_backend.utils import tree_export
from oasst_backend.utils.database_utils import CommitMode, async_managed_tx_method, managed_tx_method
from oasst_backend.utils.hugging_face import HfClassificationModel, HfEmbeddingModel, HfUrl, HuggingFaceAPI
from oasst_backend.utils.ranking import ranked_pairs
from oasst_shared.exceptions.oasst_api_error import OasstError, OasstErrorCode
from oasst_shared.schemas import protocol as protocol_schema
from sqlmodel import Session, func, not_, or_, text, update


class TaskType(Enum):
    NONE = -1
    RANKING = 0
    LABEL_REPLY = 1
    REPLY = 2
    LABEL_PROMPT = 3
    PROMPT = 4


class TaskRole(Enum):
    ANY = 0
    PROMPTER = 1
    ASSISTANT = 2


class ActiveTreeSizeRow(pydantic.BaseModel):
    message_tree_id: UUID
    goal_tree_size: int
    tree_size: int
    awaiting_review: Optional[int]

    @property
    def remaining_messages(self) -> int:
        return max(0, self.goal_tree_size - self.tree_size)

    class Config:
        orm_mode = True


class ExtendibleParentRow(pydantic.BaseModel):
    parent_id: UUID
    parent_role: str
    depth: int
    message_tree_id: UUID
    active_children_count: int

    class Config:
        orm_mode = True


class IncompleteRankingsRow(pydantic.BaseModel):
    parent_id: UUID
    role: str
    children_count: int
    child_min_ranking_count: int
    message_tree_id: UUID

    class Config:
        orm_mode = True


class TreeMessageCountStats(pydantic.BaseModel):
    message_tree_id: UUID
    state: str
    depth: int
    oldest: datetime
    youngest: datetime
    count: int
    goal_tree_size: int

    @property
    def completed(self) -> int:
        return self.count / self.goal_tree_size


class TreeManagerStats(pydantic.BaseModel):
    state_counts: dict[str, int]
    message_counts: list[TreeMessageCountStats]


class TreeManager:
    def __init__(
        self,
        db: Session,
        prompt_repository: PromptRepository,
        cfg: Optional[TreeManagerConfiguration] = None,
    ):
        self.db = db
        self.cfg = cfg or settings.tree_manager
        self.pr = prompt_repository

    def _random_task_selection(
        self,
        num_ranking_tasks: int,
        num_replies_need_review: int,
        num_prompts_need_review: int,
        num_missing_prompts: int,
        num_missing_replies: int,
    ) -> TaskType:
        """
        Determines which task to hand out to human worker.
        The task type is drawn with relative weight (e.g. ranking has highest priority)
        depending on what is possible with the current message trees in the database.
        """

        logger.debug(
            f"TreeManager._random_task_selection({num_ranking_tasks=}, {num_replies_need_review=}, "
            f"{num_prompts_need_review=}, {num_missing_prompts=}, {num_missing_replies=})"
        )

        task_type = TaskType.NONE
        task_weights = [0] * 5

        if num_ranking_tasks > 0:
            task_weights[TaskType.RANKING.value] = 10

        if num_replies_need_review > 0:
            task_weights[TaskType.LABEL_REPLY.value] = 5

        if num_prompts_need_review > 0:
            task_weights[TaskType.LABEL_PROMPT.value] = 5

        if num_missing_replies > 0:
            task_weights[TaskType.REPLY.value] = 2

        if num_missing_prompts > 0:
            task_weights[TaskType.PROMPT.value] = 1

        task_weights = np.array(task_weights)
        weight_sum = task_weights.sum()
        if weight_sum > 1e-8:
            task_weights = task_weights / weight_sum
            task_type = TaskType(np.random.choice(a=len(task_weights), p=task_weights))

        logger.debug(f"Selected {task_type=}")
        return task_type

    def _determine_task_availability_internal(
        self,
        num_active_trees: int,
        extendible_parents: list[ExtendibleParentRow],
        prompts_need_review: list[Message],
        replies_need_review: list[Message],
        incomplete_rankings: list[IncompleteRankingsRow],
    ) -> dict[protocol_schema.TaskRequestType, int]:
        task_count_by_type: dict[protocol_schema.TaskRequestType, int] = {t: 0 for t in protocol_schema.TaskRequestType}

        num_missing_prompts = max(0, self.cfg.max_active_trees - num_active_trees)
        task_count_by_type[protocol_schema.TaskRequestType.initial_prompt] = num_missing_prompts

        task_count_by_type[protocol_schema.TaskRequestType.prompter_reply] = len(
            list(filter(lambda x: x.parent_role == "assistant", extendible_parents))
        )
        task_count_by_type[protocol_schema.TaskRequestType.assistant_reply] = len(
            list(filter(lambda x: x.parent_role == "prompter", extendible_parents))
        )

        task_count_by_type[protocol_schema.TaskRequestType.label_initial_prompt] = len(prompts_need_review)
        task_count_by_type[protocol_schema.TaskRequestType.label_assistant_reply] = len(
            list(filter(lambda m: m.role == "assistant", replies_need_review))
        )
        task_count_by_type[protocol_schema.TaskRequestType.label_prompter_reply] = len(
            list(filter(lambda m: m.role == "prompter", replies_need_review))
        )

        if self.cfg.rank_prompter_replies:
            task_count_by_type[protocol_schema.TaskRequestType.rank_prompter_replies] = len(
                list(filter(lambda r: r.role == "prompter", incomplete_rankings))
            )

        task_count_by_type[protocol_schema.TaskRequestType.rank_assistant_replies] = len(
            list(filter(lambda r: r.role == "assistant", incomplete_rankings))
        )

        task_count_by_type[protocol_schema.TaskRequestType.random] = sum(
            task_count_by_type[t] for t in protocol_schema.TaskRequestType if t in task_count_by_type
        )

        return task_count_by_type

    def determine_task_availability(self, lang: str) -> dict[protocol_schema.TaskRequestType, int]:
        self.pr.ensure_user_is_enabled()

        if not lang:
            lang = "en"
            logger.warning("Task availability request without lang tag received, assuming lang='en'.")

        num_active_trees = self.query_num_active_trees(lang=lang, exclude_ranking=True)
        extendible_parents, _ = self.query_extendible_parents(lang=lang)
        prompts_need_review = self.query_prompts_need_review(lang=lang)
        replies_need_review = self.query_replies_need_review(lang=lang)
        incomplete_rankings = self.query_incomplete_rankings(lang=lang)

        return self._determine_task_availability_internal(
            num_active_trees=num_active_trees,
            extendible_parents=extendible_parents,
            prompts_need_review=prompts_need_review,
            replies_need_review=replies_need_review,
            incomplete_rankings=incomplete_rankings,
        )

    @staticmethod
    def _get_label_descriptions(valid_labels: list[TextLabels]) -> list[protocol_schema.LabelDescription]:
        return [
            protocol_schema.LabelDescription(
                name=l.value, widget=l.widget.value, display_text=l.display_text, help_text=l.help_text
            )
            for l in valid_labels
        ]

    def next_task(
        self,
        desired_task_type: protocol_schema.TaskRequestType = protocol_schema.TaskRequestType.random,
        lang: str = "en",
    ) -> Tuple[protocol_schema.Task, Optional[UUID], Optional[UUID]]:

        logger.debug(f"TreeManager.next_task({desired_task_type=}, {lang=})")

        self.pr.ensure_user_is_enabled()

        if not lang:
            lang = "en"
            logger.warning("Task request without lang tag received, assuming 'en'.")

        num_active_trees = self.query_num_active_trees(lang=lang, exclude_ranking=True)
        prompts_need_review = self.query_prompts_need_review(lang=lang)
        replies_need_review = self.query_replies_need_review(lang=lang)
        extendible_parents, active_tree_sizes = self.query_extendible_parents(lang=lang)

        incomplete_rankings = self.query_incomplete_rankings(lang=lang)
        if not self.cfg.rank_prompter_replies:
            incomplete_rankings = list(filter(lambda r: r.role == "assistant", incomplete_rankings))

        # determine type of task to generate
        num_missing_replies = sum(x.remaining_messages for x in active_tree_sizes)

        task_role = TaskRole.ANY
        if desired_task_type == protocol_schema.TaskRequestType.random:
            task_type = self._random_task_selection(
                num_ranking_tasks=len(incomplete_rankings),
                num_replies_need_review=len(replies_need_review),
                num_prompts_need_review=len(prompts_need_review),
                num_missing_prompts=max(0, self.cfg.max_active_trees - num_active_trees),
                num_missing_replies=num_missing_replies,
            )

            if task_type == TaskType.NONE:
                raise OasstError(
                    f"No tasks of type '{protocol_schema.TaskRequestType.random.value}' are currently available.",
                    OasstErrorCode.TASK_REQUESTED_TYPE_NOT_AVAILABLE,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
        else:
            task_count_by_type = self._determine_task_availability_internal(
                num_active_trees=num_active_trees,
                extendible_parents=extendible_parents,
                prompts_need_review=prompts_need_review,
                replies_need_review=replies_need_review,
                incomplete_rankings=incomplete_rankings,
            )

            available_count = task_count_by_type.get(desired_task_type)
            if not available_count:
                raise OasstError(
                    f"No tasks of type '{desired_task_type.value}' are currently available.",
                    OasstErrorCode.TASK_REQUESTED_TYPE_NOT_AVAILABLE,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )

            task_type_role_map = {
                protocol_schema.TaskRequestType.initial_prompt: (TaskType.PROMPT, TaskRole.ANY),
                protocol_schema.TaskRequestType.prompter_reply: (TaskType.REPLY, TaskRole.PROMPTER),
                protocol_schema.TaskRequestType.assistant_reply: (TaskType.REPLY, TaskRole.ASSISTANT),
                protocol_schema.TaskRequestType.rank_prompter_replies: (TaskType.RANKING, TaskRole.PROMPTER),
                protocol_schema.TaskRequestType.rank_assistant_replies: (TaskType.RANKING, TaskRole.ASSISTANT),
                protocol_schema.TaskRequestType.label_initial_prompt: (TaskType.LABEL_PROMPT, TaskRole.ANY),
                protocol_schema.TaskRequestType.label_assistant_reply: (TaskType.LABEL_REPLY, TaskRole.ASSISTANT),
                protocol_schema.TaskRequestType.label_prompter_reply: (TaskType.LABEL_REPLY, TaskRole.PROMPTER),
            }

            task_type, task_role = task_type_role_map[desired_task_type]

        message_tree_id = None
        parent_message_id = None

        logger.debug(f"selected {task_type=}")
        match task_type:
            case TaskType.RANKING:
                if task_role == TaskRole.PROMPTER:
                    incomplete_rankings = list(filter(lambda m: m.role == "prompter", incomplete_rankings))
                elif task_role == TaskRole.ASSISTANT:
                    incomplete_rankings = list(filter(lambda m: m.role == "assistant", incomplete_rankings))

                if len(incomplete_rankings) > 0:
                    ranking_parent_id = random.choice(incomplete_rankings).parent_id

                    messages = self.pr.fetch_message_conversation(ranking_parent_id)
                    assert len(messages) > 0 and messages[-1].id == ranking_parent_id
                    ranking_parent = messages[-1]
                    assert not ranking_parent.deleted and ranking_parent.review_result
                    conversation = prepare_conversation(messages)
                    replies = self.pr.fetch_message_children(ranking_parent_id, review_result=True, deleted=False)

                    assert len(replies) > 1
                    random.shuffle(replies)  # hand out replies in random order
                    reply_messages = prepare_conversation_message_list(replies)
                    replies = [p.text for p in replies]

                    if messages[-1].role == "assistant":
                        logger.info("Generating a RankPrompterRepliesTask.")
                        task = protocol_schema.RankPrompterRepliesTask(
                            conversation=conversation,
                            replies=replies,
                            reply_messages=reply_messages,
                            ranking_parent_id=ranking_parent.id,
                            message_tree_id=ranking_parent.message_tree_id,
                        )
                    else:
                        logger.info("Generating a RankAssistantRepliesTask.")
                        task = protocol_schema.RankAssistantRepliesTask(
                            conversation=conversation,
                            replies=replies,
                            reply_messages=reply_messages,
                            ranking_parent_id=ranking_parent.id,
                            message_tree_id=ranking_parent.message_tree_id,
                        )

                    parent_message_id = ranking_parent_id
                    message_tree_id = messages[-1].message_tree_id

            case TaskType.LABEL_REPLY:

                if task_role == TaskRole.PROMPTER:
                    replies_need_review = list(filter(lambda m: m.role == "prompter", replies_need_review))
                elif task_role == TaskRole.ASSISTANT:
                    replies_need_review = list(filter(lambda m: m.role == "assistant", replies_need_review))

                if len(replies_need_review) > 0:
                    random_reply_message = random.choice(replies_need_review)
                    messages = self.pr.fetch_message_conversation(random_reply_message)

                    conversation = prepare_conversation(messages)
                    message = messages[-1]

                    self.cfg.p_full_labeling_review_reply_prompter: float = 0.1

                    label_mode = protocol_schema.LabelTaskMode.full
                    label_disposition = protocol_schema.LabelTaskDisposition.quality

                    if message.role == "assistant":
                        valid_labels = self.cfg.labels_assistant_reply
                        if (
                            desired_task_type == protocol_schema.TaskRequestType.random
                            and random.random() > self.cfg.p_full_labeling_review_reply_assistant
                        ):
                            label_mode = protocol_schema.LabelTaskMode.simple
                            label_disposition = protocol_schema.LabelTaskDisposition.spam
                            valid_labels = self.cfg.mandatory_labels_assistant_reply.copy()
                            if protocol_schema.TextLabel.lang_mismatch not in valid_labels:
                                valid_labels.append(protocol_schema.TextLabel.lang_mismatch)
                            if protocol_schema.TextLabel.quality not in valid_labels:
                                valid_labels.append(protocol_schema.TextLabel.quality)

                        logger.info(f"Generating a LabelAssistantReplyTask. ({label_mode=:s})")
                        task = protocol_schema.LabelAssistantReplyTask(
                            message_id=message.id,
                            conversation=conversation,
                            reply=message.text,
                            reply_message=prepare_conversation_message(message),
                            valid_labels=list(map(lambda x: x.value, valid_labels)),
                            mandatory_labels=list(map(lambda x: x.value, self.cfg.mandatory_labels_assistant_reply)),
                            mode=label_mode,
                            disposition=label_disposition,
                            labels=self._get_label_descriptions(valid_labels),
                        )
                    else:
                        valid_labels = self.cfg.labels_prompter_reply
                        if (
                            desired_task_type == protocol_schema.TaskRequestType.random
                            and random.random() > self.cfg.p_full_labeling_review_reply_prompter
                        ):
                            label_mode = protocol_schema.LabelTaskMode.simple
                            label_disposition = protocol_schema.LabelTaskDisposition.spam
                            valid_labels = self.cfg.mandatory_labels_prompter_reply.copy()
                            if protocol_schema.TextLabel.lang_mismatch not in valid_labels:
                                valid_labels.append(protocol_schema.TextLabel.lang_mismatch)
                            if protocol_schema.TextLabel.quality not in valid_labels:
                                valid_labels.append(protocol_schema.TextLabel.quality)

                        logger.info(f"Generating a LabelPrompterReplyTask. ({label_mode=:s})")
                        task = protocol_schema.LabelPrompterReplyTask(
                            message_id=message.id,
                            conversation=conversation,
                            reply=message.text,
                            reply_message=prepare_conversation_message(message),
                            valid_labels=list(map(lambda x: x.value, valid_labels)),
                            mandatory_labels=list(map(lambda x: x.value, self.cfg.mandatory_labels_prompter_reply)),
                            mode=label_mode,
                            disposition=label_disposition,
                            labels=self._get_label_descriptions(valid_labels),
                        )

                    parent_message_id = message.id
                    message_tree_id = message.message_tree_id

            case TaskType.REPLY:

                recent_reply_tasks = self.pr.task_repository.fetch_recent_reply_tasks(
                    max_age=timedelta(seconds=self.cfg.recent_tasks_span_sec), done=False
                )
                recent_reply_task_parents = {t.parent_message_id for t in recent_reply_tasks}

                if task_role == TaskRole.PROMPTER:
                    extendible_parents = list(filter(lambda x: x.parent_role == "assistant", extendible_parents))
                elif task_role == TaskRole.ASSISTANT:
                    extendible_parents = list(filter(lambda x: x.parent_role == "prompter", extendible_parents))

                # select a tree with missing replies
                if len(extendible_parents) > 0:
                    random_parent: ExtendibleParentRow = None
                    if self.cfg.p_lonely_child_extension > 0 and self.cfg.lonely_children_count > 1:
                        # check if we have extendible parents with a small number of replies

                        lonely_children_parents = [
                            p
                            for p in extendible_parents
                            if 0 < p.active_children_count < self.cfg.lonely_children_count
                            and p.parent_id not in recent_reply_task_parents
                        ]
                        if len(lonely_children_parents) > 0 and random.random() < self.cfg.p_lonely_child_extension:
                            random_parent = random.choice(lonely_children_parents)

                    if random_parent is None:
                        # try to exclude parents for which tasks were recently handed out
                        fresh_parents = [p for p in extendible_parents if p.parent_id not in recent_reply_task_parents]
                        if len(fresh_parents) > 0:
                            random_parent = random.choice(fresh_parents)
                        else:
                            random_parent = random.choice(extendible_parents)

                    # fetch random conversation to extend
                    logger.debug(f"selected {random_parent=}")
                    messages = self.pr.fetch_message_conversation(random_parent.parent_id)
                    assert all(m.review_result for m in messages)  # ensure all messages have positive reviews
                    conversation = prepare_conversation(messages)

                    # generate reply task depending on last message
                    if messages[-1].role == "assistant":
                        logger.info("Generating a PrompterReplyTask.")
                        task = protocol_schema.PrompterReplyTask(conversation=conversation)
                    else:
                        logger.info("Generating a AssistantReplyTask.")
                        task = protocol_schema.AssistantReplyTask(conversation=conversation)

                    parent_message_id = messages[-1].id
                    message_tree_id = messages[-1].message_tree_id

            case TaskType.LABEL_PROMPT:
                assert len(prompts_need_review) > 0
                message = random.choice(prompts_need_review)

                label_mode = protocol_schema.LabelTaskMode.full
                label_disposition = protocol_schema.LabelTaskDisposition.quality
                valid_labels = self.cfg.labels_initial_prompt

                if random.random() > self.cfg.p_full_labeling_review_prompt:
                    valid_labels = self.cfg.mandatory_labels_initial_prompt.copy()
                    label_mode = protocol_schema.LabelTaskMode.simple
                    label_disposition = protocol_schema.LabelTaskDisposition.spam
                    if protocol_schema.TextLabel.lang_mismatch not in valid_labels:
                        valid_labels.append(protocol_schema.TextLabel.lang_mismatch)

                logger.info(f"Generating a LabelInitialPromptTask ({label_mode=:s}).")
                task = protocol_schema.LabelInitialPromptTask(
                    message_id=message.id,
                    prompt=message.text,
                    valid_labels=list(map(lambda x: x.value, valid_labels)),
                    mandatory_labels=list(map(lambda x: x.value, self.cfg.mandatory_labels_initial_prompt)),
                    mode=label_mode,
                    disposition=label_disposition,
                    labels=self._get_label_descriptions(valid_labels),
                )

                parent_message_id = message.id
                message_tree_id = message.message_tree_id

            case TaskType.PROMPT:
                logger.info("Generating an InitialPromptTask.")
                task = protocol_schema.InitialPromptTask(hint=None)

            case _:
                task = None

        if task is None:
            raise OasstError(
                f"No task of type '{desired_task_type.value}' is currently available.",
                OasstErrorCode.TASK_REQUESTED_TYPE_NOT_AVAILABLE,
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

        logger.info(f"Generated {task=}.")

        return task, message_tree_id, parent_message_id

    @async_managed_tx_method(CommitMode.COMMIT)
    async def handle_interaction(self, interaction: protocol_schema.AnyInteraction) -> protocol_schema.Task:
        pr = self.pr
        pr.ensure_user_is_enabled()
        match type(interaction):
            case protocol_schema.TextReplyToMessage:
                logger.info(
                    f"Frontend reports text reply to {interaction.message_id=} with {interaction.text=} by {interaction.user=}."
                )
                # here we store the text reply in the database
                message = pr.store_text_reply(
                    text=interaction.text,
                    lang=interaction.lang,
                    frontend_message_id=interaction.message_id,
                    user_frontend_message_id=interaction.user_message_id,
                )

                if not message.parent_id:
                    logger.info(f"TreeManager: Inserting new tree state for initial prompt {message.id=}")
                    self._insert_default_state(message.id)

                if not settings.DEBUG_SKIP_EMBEDDING_COMPUTATION:
                    try:
                        hugging_face_api = HuggingFaceAPI(
                            f"{HfUrl.HUGGINGFACE_FEATURE_EXTRACTION.value}/{HfEmbeddingModel.MINILM.value}"
                        )
                        embedding = await hugging_face_api.post(interaction.text)
                        pr.insert_message_embedding(
                            message_id=message.id, model=HfEmbeddingModel.MINILM.value, embedding=embedding
                        )
                    except OasstError:
                        logger.error(
                            f"Could not fetch embbeddings for text reply to {interaction.message_id=} with {interaction.text=} by {interaction.user=}."
                        )

                if not settings.DEBUG_SKIP_TOXICITY_CALCULATION:
                    try:
                        model_name: str = HfClassificationModel.TOXIC_ROBERTA.value
                        hugging_face_api: HuggingFaceAPI = HuggingFaceAPI(
                            f"{HfUrl.HUGGINGFACE_TOXIC_CLASSIFICATION.value}/{model_name}"
                        )

                        toxicity: List[List[Dict[str, Any]]] = await hugging_face_api.post(interaction.text)
                        toxicity = toxicity[0][0]
                        pr.insert_toxicity(
                            message_id=message.id, model=model_name, score=toxicity["score"], label=toxicity["label"]
                        )

                    except OasstError:
                        logger.error(
                            f"Could not compute toxicity for text reply to {interaction.message_id=} with {interaction.text=} by {interaction.user=}."
                        )

            case protocol_schema.MessageRating:
                logger.info(
                    f"Frontend reports rating of {interaction.message_id=} with {interaction.rating=} by {interaction.user=}."
                )

                pr.store_rating(interaction)

            case protocol_schema.MessageRanking:
                logger.info(
                    f"Frontend reports ranking of {interaction.message_id=} with {interaction.ranking=} by {interaction.user=}."
                )

                _, task = pr.store_ranking(interaction)
                self.check_condition_for_scoring_state(task.message_tree_id)

            case protocol_schema.TextLabels:
                logger.info(
                    f"Frontend reports labels of {interaction.message_id=} with {interaction.labels=} by {interaction.user=}."
                )

                _, task, msg = pr.store_text_labels(interaction)

                # if it was a response for a task, check if we have enough reviews to calc review_result
                if task and msg:
                    reviews = self.query_reviews_for_message(msg.id)
                    acceptance_score = self._calculate_acceptance(reviews)
                    logger.debug(
                        f"Message {msg.id=}, {acceptance_score=}, {len(reviews)=}, {msg.review_result=}, {msg.review_count=}"
                    )
                    if msg.parent_id is None:
                        if not msg.review_result and msg.review_count >= self.cfg.num_reviews_initial_prompt:
                            if acceptance_score > self.cfg.acceptance_threshold_initial_prompt:
                                msg.review_result = True
                                self.db.add(msg)
                                logger.info(
                                    f"Initial prompt message was accepted: {msg.id=}, {acceptance_score=}, {len(reviews)=}"
                                )
                            else:
                                self.enter_low_grade_state(msg.message_tree_id)
                        self.check_condition_for_growing_state(msg.message_tree_id)
                    elif msg.review_count >= self.cfg.num_reviews_reply:
                        if not msg.review_result and acceptance_score > self.cfg.acceptance_threshold_reply:
                            msg.review_result = True
                            self.db.add(msg)
                            logger.info(
                                f"Reply message message accepted: {msg.id=}, {acceptance_score=}, {len(reviews)=}"
                            )

                    self.check_condition_for_ranking_state(msg.message_tree_id)

            case _:
                raise OasstError("Invalid response type.", OasstErrorCode.TASK_INVALID_RESPONSE_TYPE)

        return protocol_schema.TaskDone()

    def _enter_state(self, mts: MessageTreeState, state: message_tree_state.State):
        assert mts

        is_terminal = state in message_tree_state.TERMINAL_STATES
        was_active = mts.active
        if is_terminal:
            mts.active = False
        mts.state = state.value
        self.db.add(mts)
        self.db.flush

        if is_terminal:
            logger.info(f"Tree entered terminal '{mts.state}' state ({mts.message_tree_id=})")
            root_msg = self.pr.fetch_message(message_id=mts.message_tree_id, fail_if_missing=False)
            if root_msg and was_active:
                if random.random() < self.cfg.p_activate_backlog_tree:
                    self.activate_backlog_tree(lang=root_msg.lang)

                if self.cfg.min_active_rankings_per_lang > 0:
                    incomplete_rankings = self.query_incomplete_rankings(lang=root_msg.lang)
                    if len(incomplete_rankings) < self.cfg.min_active_rankings_per_lang:
                        self.activate_backlog_tree(lang=root_msg.lang)
        else:
            logger.info(f"Tree entered '{mts.state}' state ({mts.message_tree_id=})")

    def enter_low_grade_state(self, message_tree_id: UUID) -> None:
        logger.debug(f"enter_low_grade_state({message_tree_id=})")
        mts = self.pr.fetch_tree_state(message_tree_id)
        self._enter_state(mts, message_tree_state.State.ABORTED_LOW_GRADE)

    def check_condition_for_growing_state(self, message_tree_id: UUID) -> bool:
        logger.debug(f"check_condition_for_growing_state({message_tree_id=})")

        mts = self.pr.fetch_tree_state(message_tree_id)
        if not mts.active or mts.state != message_tree_state.State.INITIAL_PROMPT_REVIEW:
            logger.debug(f"False {mts.active=}, {mts.state=}")
            return False

        # check if initial prompt was accepted
        initial_prompt = self.pr.fetch_message(message_tree_id)
        if not initial_prompt.review_result:
            logger.debug(f"False {initial_prompt.review_result=}")
            return False

        self._enter_state(mts, message_tree_state.State.GROWING)
        return True

    def check_condition_for_ranking_state(self, message_tree_id: UUID) -> bool:
        logger.debug(f"check_condition_for_ranking_state({message_tree_id=})")

        mts = self.pr.fetch_tree_state(message_tree_id)
        if not mts.active or mts.state != message_tree_state.State.GROWING:
            logger.debug(f"False {mts.active=}, {mts.state=}")
            return False

        # check if desired tree size has been reached and all nodes have been reviewed
        tree_size = self.query_tree_size(message_tree_id)
        if tree_size.remaining_messages > 0 or tree_size.awaiting_review > 0:
            logger.debug(f"False {tree_size.remaining_messages=}, {tree_size.awaiting_review=}")
            return False

        self._enter_state(mts, message_tree_state.State.RANKING)
        return True

    def check_condition_for_scoring_state(self, message_tree_id: UUID) -> bool:
        logger.debug(f"check_condition_for_scoring_state({message_tree_id=})")

        mts = self.pr.fetch_tree_state(message_tree_id)
        if mts.state != message_tree_state.State.SCORING_FAILED:
            if not mts.active or mts.state not in (
                message_tree_state.State.RANKING,
                message_tree_state.State.READY_FOR_SCORING,
            ):
                logger.debug(f"False {mts.active=}, {mts.state=}")
                return False

        ranking_role_filter = None if self.cfg.rank_prompter_replies else "assistant"
        rankings_by_message = self.query_tree_ranking_results(message_tree_id, role_filter=ranking_role_filter)
        for parent_msg_id, ranking in rankings_by_message.items():
            if len(ranking) < self.cfg.num_required_rankings:
                logger.debug(f"False {parent_msg_id=} {len(ranking)=}")
                return False

        if (
            mts.state != message_tree_state.State.SCORING_FAILED
            and mts.state != message_tree_state.State.READY_FOR_SCORING
        ):
            self._enter_state(mts, message_tree_state.State.READY_FOR_SCORING)
        self.update_message_ranks(message_tree_id, rankings_by_message)
        return True

    def update_message_ranks(
        self, message_tree_id: UUID, rankings_by_message: dict[UUID, list[MessageReaction]]
    ) -> bool:

        mts = self.pr.fetch_tree_state(message_tree_id)
        # check state, allow retry if in SCORING_FAILED state
        if mts.state not in (message_tree_state.State.READY_FOR_SCORING, message_tree_state.State.SCORING_FAILED):
            logger.debug(f"False {mts.active=}, {mts.state=}")
            return False

        if mts.state == message_tree_state.State.SCORING_FAILED:
            mts.active = True
            mts.state = message_tree_state.State.READY_FOR_SCORING

        try:
            for rankings in rankings_by_message.values():
                ordered_ids_list: list[list[UUID]] = [
                    msg_reaction.payload.payload.ranked_message_ids for msg_reaction in rankings
                ]

                common_set: set[UUID] = set.intersection(*map(set, ordered_ids_list))
                if len(common_set) < 2:
                    logger.warning("The intersection of ranking results ID sets has less than two elements. Skipping.")
                    continue

                # keep only elements in commond set
                ordered_ids_list = [list(filter(lambda x: x in common_set, ids)) for ids in ordered_ids_list]
                assert all(len(x) == len(common_set) for x in ordered_ids_list)

                logger.debug(f"SORTED MESSAGE IDS {ordered_ids_list}")
                consensus = ranked_pairs(ordered_ids_list)
                assert len(consensus) == len(common_set)
                logger.debug(f"CONSENSUS: {consensus}\n\n")

                # fetch all siblings and clear ranks
                siblings = self.pr.fetch_message_siblings(consensus[0], review_result=None, deleted=None)
                for m in siblings:
                    m.rank = None
                    self.db.add(m)

                # index by id
                siblings = {m.id: m for m in siblings}

                # set rank for each message that was part of the common set
                for rank, message_id in enumerate(consensus):
                    msg = siblings.get(message_id)
                    if msg:
                        msg.rank = rank
                        self.db.add(msg)
                    else:
                        logger.warning(f"Message {message_id=} not found among siblings.")

        except Exception:
            logger.exception(f"update_message_ranks({message_tree_id=}) failed")
            self._enter_state(mts, message_tree_state.State.SCORING_FAILED)
            return False

        self._enter_state(mts, message_tree_state.State.READY_FOR_EXPORT)

        return True

    def activate_backlog_tree(self, lang: str) -> MessageTreeState:
        while True:
            # find tree in backlog state
            backlog_tree: MessageTreeState = (
                self.db.query(MessageTreeState)
                .join(Message, MessageTreeState.message_tree_id == Message.id)  # root msg
                .filter(MessageTreeState.state == message_tree_state.State.BACKLOG_RANKING)
                .filter(Message.lang == lang)
                .limit(1)
                .one_or_none()
            )

            if not backlog_tree:
                return None

            if len(self.query_tree_ranking_results(message_tree_id=backlog_tree.message_tree_id)) == 0:
                logger.info(
                    f"Backlog tree {backlog_tree.message_tree_id} has no children to rank, aborting with 'aborted_low_grade' state."
                )
                self._enter_state(backlog_tree, message_tree_state.State.ABORTED_LOW_GRADE)
            else:
                logger.info(f"Activating backlog tree {backlog_tree.message_tree_id}")
                backlog_tree.active = True
                self._enter_state(backlog_tree, message_tree_state.State.RANKING)
                return backlog_tree

    def _calculate_acceptance(self, labels: list[TextLabels]):
        # calculate acceptance based on lang_mismatch & spam label
        lang_mismatch = np.mean([(l.labels.get(protocol_schema.TextLabel.lang_mismatch) or 0) for l in labels])
        spam = np.mean([l.labels[protocol_schema.TextLabel.spam] for l in labels])
        acceptance_score = 1 - (spam + lang_mismatch)
        logger.debug(f"{acceptance_score=} ({spam=}, {lang_mismatch=})")
        return acceptance_score

    def _query_need_review(
        self, state: message_tree_state.State, required_reviews: int, root: bool, lang: str
    ) -> list[Message]:

        need_review = (
            self.db.query(Message)
            .select_from(MessageTreeState)
            .join(Message, MessageTreeState.message_tree_id == Message.message_tree_id)
            .filter(
                MessageTreeState.active,
                MessageTreeState.state == state,
                not_(Message.review_result),
                not_(Message.deleted),
                Message.review_count < required_reviews,
                Message.lang == lang,
            )
        )

        if root:
            need_review = need_review.filter(Message.parent_id.is_(None))
        else:
            need_review = need_review.filter(Message.parent_id.is_not(None))

        if not settings.DEBUG_ALLOW_SELF_LABELING:
            need_review = need_review.filter(Message.user_id != self.pr.user_id)

        if settings.DEBUG_ALLOW_DUPLICATE_TASKS:
            qry = need_review
        else:
            user_id = self.pr.user_id
            need_review = need_review.cte(name="need_review")
            qry = (
                self.db.query(Message)
                .select_entity_from(need_review)
                .outerjoin(TextLabels, need_review.c.id == TextLabels.message_id)
                .group_by(need_review)
                .having(
                    func.count(TextLabels.id).filter(TextLabels.task_id.is_not(None), TextLabels.user_id == user_id)
                    == 0
                )
            )

        return qry.all()

    def query_prompts_need_review(self, lang: str) -> list[Message]:
        """
        Select initial prompt messages with less then required rankings in active message tree
        (active == True in message_tree_state)
        """
        return self._query_need_review(
            message_tree_state.State.INITIAL_PROMPT_REVIEW, self.cfg.num_reviews_initial_prompt, True, lang
        )

    def query_replies_need_review(self, lang: str) -> list[Message]:
        """
        Select child messages (parent_id IS NOT NULL) with less then required rankings
        in active message tree (active == True in message_tree_state)
        """
        return self._query_need_review(message_tree_state.State.GROWING, self.cfg.num_reviews_reply, False, lang)

    _sql_find_incomplete_rankings = """
-- find incomplete rankings
SELECT m.parent_id, m.role, COUNT(m.id) children_count, MIN(m.ranking_count) child_min_ranking_count,
    COUNT(m.id) FILTER (WHERE m.ranking_count >= :num_required_rankings) as completed_rankings,
    mts.message_tree_id
FROM message_tree_state mts
    INNER JOIN message m ON mts.message_tree_id = m.message_tree_id
WHERE mts.active                        -- only consider active trees
    AND mts.state = :ranking_state      -- message tree must be in ranking state
    AND m.review_result                 -- must be reviewed
    AND m.lang = :lang                  -- matches lang
    AND NOT m.deleted                   -- not deleted
    AND m.parent_id IS NOT NULL         -- ignore initial prompts
GROUP BY m.parent_id, m.role, mts.message_tree_id
HAVING COUNT(m.id) > 1 and MIN(m.ranking_count) < :num_required_rankings
"""

    _sql_find_incomplete_rankings_ex = f"""
-- incomplete rankings but exclude of current user
WITH incomplete_rankings AS ({_sql_find_incomplete_rankings})
SELECT ir.* FROM incomplete_rankings ir
    LEFT JOIN message_reaction mr ON ir.parent_id = mr.message_id AND mr.payload_type = 'RankingReactionPayload'
GROUP BY ir.parent_id, ir.role, ir.children_count, ir.child_min_ranking_count, ir.completed_rankings,
    ir.message_tree_id
HAVING(COUNT(mr.message_id) FILTER (WHERE mr.user_id = :user_id) = 0)
"""

    def query_incomplete_rankings(self, lang: str) -> list[IncompleteRankingsRow]:
        """Query parents which have childern that need further rankings"""

        user_id = self.pr.user_id if not settings.DEBUG_ALLOW_DUPLICATE_TASKS else None
        r = self.db.execute(
            text(self._sql_find_incomplete_rankings_ex),
            {
                "num_required_rankings": self.cfg.num_required_rankings,
                "ranking_state": message_tree_state.State.RANKING,
                "lang": lang,
                "user_id": user_id,
            },
        )
        return [IncompleteRankingsRow.from_orm(x) for x in r.all()]

    _sql_find_extendible_parents = """
-- find all extendible parent nodes
SELECT m.id as parent_id, m.role as parent_role, m.depth, m.message_tree_id, COUNT(c.id) active_children_count
FROM message_tree_state mts
    INNER JOIN message m ON mts.message_tree_id = m.message_tree_id	-- all elements of message tree
    LEFT JOIN message c ON m.id = c.parent_id  -- child nodes
WHERE mts.active                        -- only consider active trees
    AND mts.state = :growing_state      -- message tree must be growing
    AND NOT m.deleted                   -- ignore deleted messages as parents
    AND m.depth < mts.max_depth         -- ignore leaf nodes as parents
    AND m.review_result                 -- parent node must have positive review
    AND m.lang = :lang                  -- parent matches lang
    AND NOT coalesce(c.deleted, FALSE)  -- don't count deleted children
    AND (c.review_result OR coalesce(c.review_count, 0) < :num_reviews_reply) -- don't count children with negative review but count elements under review
GROUP BY m.id, m.role, m.depth, m.message_tree_id, mts.max_children_count
HAVING COUNT(c.id) < mts.max_children_count -- below maximum number of children
    AND COUNT(c.id) FILTER (WHERE c.user_id = :user_id) = 0  -- without reply by user
"""

    def query_extendible_parents(self, lang: str) -> tuple[list[ExtendibleParentRow], list[ActiveTreeSizeRow]]:
        """Query parent messages that have not reached the maximum number of replies."""

        user_id = self.pr.user_id if not settings.DEBUG_ALLOW_DUPLICATE_TASKS else None
        r = self.db.execute(
            text(self._sql_find_extendible_parents),
            {
                "growing_state": message_tree_state.State.GROWING,
                "num_reviews_reply": self.cfg.num_reviews_reply,
                "lang": lang,
                "user_id": user_id,
            },
        )

        potential_parents = [ExtendibleParentRow.from_orm(x) for x in r.all()]
        extendible_trees = self.query_extendible_trees(lang=lang)
        extendible_tree_ids = set(t.message_tree_id for t in extendible_trees)
        extendible_parents = list(p for p in potential_parents if p.message_tree_id in extendible_tree_ids)

        return extendible_parents, extendible_trees

    _sql_find_extendible_trees = f"""
-- find extendible trees
SELECT m.message_tree_id, mts.goal_tree_size, COUNT(m.id) AS tree_size
FROM (
        SELECT DISTINCT message_tree_id FROM ({_sql_find_extendible_parents}) extendible_parents
    ) trees INNER JOIN message_tree_state mts ON trees.message_tree_id = mts.message_tree_id
    INNER JOIN message m ON mts.message_tree_id = m.message_tree_id
WHERE NOT m.deleted
    AND (
        m.parent_id IS NOT NULL AND (m.review_result OR m.review_count < :num_reviews_reply) -- children
        OR m.parent_id IS NULL AND m.review_result -- prompts (root nodes) must have positive review
    )
GROUP BY m.message_tree_id, mts.goal_tree_size
HAVING COUNT(m.id) < mts.goal_tree_size
"""

    def query_extendible_trees(self, lang: str) -> list[ActiveTreeSizeRow]:
        """Query size of active message trees in growing state."""

        user_id = self.pr.user_id if not settings.DEBUG_ALLOW_DUPLICATE_TASKS else None
        r = self.db.execute(
            text(self._sql_find_extendible_trees),
            {
                "growing_state": message_tree_state.State.GROWING,
                "num_reviews_reply": self.cfg.num_reviews_reply,
                "lang": lang,
                "user_id": user_id,
            },
        )
        return [ActiveTreeSizeRow.from_orm(x) for x in r.all()]

    def query_tree_size(self, message_tree_id: UUID) -> ActiveTreeSizeRow:
        """Returns the number of reviewed not deleted messages in the message tree."""

        required_reviews = settings.tree_manager.num_reviews_reply
        qry = (
            self.db.query(
                MessageTreeState.message_tree_id.label("message_tree_id"),
                MessageTreeState.goal_tree_size.label("goal_tree_size"),
                func.count(Message.id).filter(Message.review_result).label("tree_size"),
                func.count(Message.id)
                .filter(not_(Message.review_result), Message.review_count < required_reviews)
                .label("awaiting_review"),
            )
            .select_from(MessageTreeState)
            .outerjoin(Message, MessageTreeState.message_tree_id == Message.message_tree_id)
            .filter(
                MessageTreeState.active,
                not_(Message.deleted),
                MessageTreeState.message_tree_id == message_tree_id,
            )
            .group_by(MessageTreeState.message_tree_id, MessageTreeState.goal_tree_size)
        )

        return ActiveTreeSizeRow.from_orm(qry.one())

    def query_misssing_tree_states(self) -> list[UUID]:
        """Find all initial prompt messages that have no associated message tree state"""
        qry_missing_tree_states = (
            self.db.query(Message.id)
            .outerjoin(MessageTreeState, Message.message_tree_id == MessageTreeState.message_tree_id)
            .filter(
                Message.parent_id.is_(None),
                Message.message_tree_id == Message.id,
                MessageTreeState.message_tree_id.is_(None),
            )
        )

        return [m.id for m in qry_missing_tree_states.all()]

    _sql_find_tree_ranking_results = """
-- get all ranking results of completed tasks for all parents with >= 2 children
SELECT p.parent_id, mr.* FROM
(
    -- find parents with > 1 children
    SELECT m.parent_id, m.message_tree_id, COUNT(m.id) children_count
    FROM message_tree_state mts
       INNER JOIN message m ON mts.message_tree_id = m.message_tree_id
    WHERE m.review_result                  -- must be reviewed
       AND NOT m.deleted                   -- not deleted
       AND m.parent_id IS NOT NULL         -- ignore initial prompts
       AND (:role IS NULL OR m.role = :role) -- children with matching role
       AND mts.message_tree_id = :message_tree_id
    GROUP BY m.parent_id, m.message_tree_id
    HAVING COUNT(m.id) > 1
) as p
LEFT JOIN task t ON p.parent_id = t.parent_message_id AND t.done AND (t.payload_type = 'RankPrompterRepliesPayload' OR t.payload_type = 'RankAssistantRepliesPayload')
LEFT JOIN message_reaction mr ON mr.task_id = t.id AND mr.payload_type = 'RankingReactionPayload'
"""

    def query_tree_ranking_results(
        self,
        message_tree_id: UUID,
        role_filter: str = "assistant",
    ) -> dict[UUID, list[MessageReaction]]:
        """Finds all completed ranking restuls for a message_tree"""

        assert role_filter in (None, "assistant", "prompter")

        r = self.db.execute(
            text(self._sql_find_tree_ranking_results),
            {
                "message_tree_id": message_tree_id,
                "role": role_filter,
            },
        )

        rankings_by_message = {}
        for x in r.all():
            parent_id = x["parent_id"]
            if parent_id not in rankings_by_message:
                rankings_by_message[parent_id] = []
            if x["task_id"]:
                rankings_by_message[parent_id].append(MessageReaction.from_orm(x))
        return rankings_by_message

    @managed_tx_method(CommitMode.COMMIT)
    def ensure_tree_states(self) -> None:
        """Add message tree state rows for all root nodes (inital prompt messages)."""

        missing_tree_ids = self.query_misssing_tree_states()
        for id in missing_tree_ids:
            tree_size = self.db.query(func.count(Message.id)).filter(Message.message_tree_id == id).scalar()
            state = message_tree_state.State.INITIAL_PROMPT_REVIEW
            if tree_size > 1:
                state = message_tree_state.State.GROWING
                logger.info(f"Inserting missing message tree state for message: {id} ({tree_size=}, {state=:s})")
            self._insert_default_state(id, state=state)

        # check tree state transitions (maybe variables haves changes): prompt review -> growing -> ranking -> scoring
        prompt_review_trees: list[MessageTreeState] = (
            self.db.query(MessageTreeState)
            .filter(MessageTreeState.state == message_tree_state.State.INITIAL_PROMPT_REVIEW, MessageTreeState.active)
            .all()
        )
        if len(prompt_review_trees) > 0:
            logger.info(
                f"Checking state of {len(prompt_review_trees)} active message trees in 'initial_prompt_review' state."
            )
            for t in prompt_review_trees:
                self.check_condition_for_growing_state(t.message_tree_id)

        growing_trees: list[MessageTreeState] = (
            self.db.query(MessageTreeState)
            .filter(MessageTreeState.state == message_tree_state.State.GROWING, MessageTreeState.active)
            .all()
        )
        if len(growing_trees) > 0:
            logger.info(f"Checking state of {len(growing_trees)} active message trees in 'growing' state.")
            for t in growing_trees:
                self.check_condition_for_ranking_state(t.message_tree_id)

        ranking_trees: list[MessageTreeState] = (
            self.db.query(MessageTreeState)
            .filter(
                or_(
                    MessageTreeState.state == message_tree_state.State.RANKING,
                    MessageTreeState.state == message_tree_state.State.READY_FOR_SCORING,
                ),
                MessageTreeState.active,
            )
            .all()
        )
        if len(ranking_trees) > 0:
            logger.info(f"Checking state of {len(ranking_trees)} active message trees in 'ranking' state.")
            for t in ranking_trees:
                self.check_condition_for_scoring_state(t.message_tree_id)

    def query_num_active_trees(self, lang: str, exclude_ranking: bool = True) -> int:
        """Count all active trees (optionally exclude those in ranking state)."""
        query = (
            self.db.query(func.count(MessageTreeState.message_tree_id))
            .join(Message, MessageTreeState.message_tree_id == Message.id)
            .filter(MessageTreeState.active, Message.lang == lang)
        )
        if exclude_ranking:
            query = query.filter(MessageTreeState.state != message_tree_state.State.RANKING)
        return query.scalar()

    def query_reviews_for_message(self, message_id: UUID) -> list[TextLabels]:
        qry = (
            self.db.query(TextLabels)
            .select_from(Task)
            .join(TextLabels, Task.id == TextLabels.id)
            .filter(Task.done, TextLabels.message_id == message_id)
        )
        return qry.all()

    @managed_tx_method(CommitMode.FLUSH)
    def _insert_tree_state(
        self,
        root_message_id: UUID,
        goal_tree_size: int,
        max_depth: int,
        max_children_count: int,
        active: bool,
        state: message_tree_state.State = message_tree_state.State.INITIAL_PROMPT_REVIEW,
    ) -> MessageTreeState:
        model = MessageTreeState(
            message_tree_id=root_message_id,
            goal_tree_size=goal_tree_size,
            max_depth=max_depth,
            max_children_count=max_children_count,
            state=state.value,
            active=active,
        )

        self.db.add(model)
        return model

    @managed_tx_method(CommitMode.FLUSH)
    def _insert_default_state(
        self,
        root_message_id: UUID,
        state: message_tree_state.State = message_tree_state.State.INITIAL_PROMPT_REVIEW,
    ) -> MessageTreeState:
        return self._insert_tree_state(
            root_message_id=root_message_id,
            goal_tree_size=self.cfg.goal_tree_size,
            max_depth=self.cfg.max_tree_depth,
            max_children_count=self.cfg.max_children_count,
            state=state,
            active=True,
        )

    def tree_counts_by_state(self) -> dict[str, int]:
        qry = self.db.query(
            MessageTreeState.state, func.count(MessageTreeState.message_tree_id).label("count")
        ).group_by(MessageTreeState.state)
        return {x["state"]: x["count"] for x in qry}

    def tree_message_count_stats(self, only_active: bool = True) -> list[TreeMessageCountStats]:
        qry = (
            self.db.query(
                MessageTreeState.message_tree_id,
                func.max(Message.depth).label("depth"),
                func.min(Message.created_date).label("oldest"),
                func.max(Message.created_date).label("youngest"),
                func.count(Message.id).label("count"),
                MessageTreeState.goal_tree_size,
                MessageTreeState.state,
            )
            .select_from(MessageTreeState)
            .join(Message, MessageTreeState.message_tree_id == Message.message_tree_id)
            .filter(not_(Message.deleted))
            .group_by(MessageTreeState.message_tree_id)
        )

        if only_active:
            qry = qry.filter(MessageTreeState.active)

        return [TreeMessageCountStats(**x) for x in qry]

    def stats(self) -> TreeManagerStats:
        return TreeManagerStats(
            state_counts=self.tree_counts_by_state(),
            message_counts=self.tree_message_count_stats(only_active=True),
        )

    def get_user_messages_by_tree(
        self,
        user_id: UUID,
        min_date: datetime = None,
        max_date: datetime = None,
    ) -> Tuple[dict[UUID, list[Message]], list[Message]]:
        """Returns a dict with replies by tree (excluding initial prompts) and list of initial prompts
        associated with user_id."""

        # query all messages of the user
        qry = self.db.query(Message).filter(Message.user_id == user_id)
        if min_date:
            qry = qry.filter(Message.created_date >= min_date)
        if max_date:
            qry = qry.filter(Message.created_date <= max_date)

        prompts: list[Message] = []
        replies_by_tree: dict[UUID, list[Message]] = {}

        # walk over result set and distinguish between initial prompts and replies
        for m in qry:
            m: Message

            if m.message_tree_id == m.id:
                prompts.append(m)
            else:
                message_list = replies_by_tree.get(m.message_tree_id)
                if message_list is None:
                    message_list = [m]
                    replies_by_tree[m.message_tree_id] = message_list
                else:
                    message_list.append(m)

        return replies_by_tree, prompts

    def _purge_message_internal(self, message_id: UUID) -> None:
        """This internal function deletes a single message. It does not take care of
        descendants, children_count in parent etc."""

        sql_purge_message = """
DELETE FROM journal j USING message m WHERE j.message_id = :message_id;
DELETE FROM message_embedding e WHERE e.message_id = :message_id;
DELETE FROM message_toxicity t WHERE t.message_id = :message_id;
DELETE FROM text_labels l WHERE l.message_id = :message_id;
-- delete all ranking results that contain message
DELETE FROM message_reaction r WHERE r.payload_type = 'RankingReactionPayload' AND r.task_id IN (
        SELECT t.id FROM message m
            JOIN task t ON m.parent_id = t.parent_message_id
        WHERE m.id = :message_id);
-- delete task which inserted message
DELETE FROM task t using message m WHERE t.id = m.task_id AND m.id = :message_id;
DELETE FROM task t WHERE t.parent_message_id = :message_id;
DELETE FROM message WHERE id = :message_id;
"""
        r = self.db.execute(text(sql_purge_message), {"message_id": message_id})
        logger.debug(f"purge_message({message_id=}): {r.rowcount} rows.")

    def purge_message_tree(self, message_tree_id: UUID) -> None:
        sql_purge_message_tree = """
DELETE FROM journal j USING message m WHERE j.message_id = m.Id AND m.message_tree_id = :message_tree_id;
DELETE FROM message_embedding e USING message m WHERE e.message_id = m.Id AND m.message_tree_id = :message_tree_id;
DELETE FROM message_toxicity t USING message m WHERE t.message_id = m.Id AND m.message_tree_id = :message_tree_id;
DELETE FROM text_labels l USING message m WHERE l.message_id = m.Id AND m.message_tree_id = :message_tree_id;
DELETE FROM message_reaction r USING task t WHERE r.task_id = t.id AND t.message_tree_id = :message_tree_id;
DELETE FROM task t WHERE t.message_tree_id = :message_tree_id;
DELETE FROM message_tree_state WHERE message_tree_id = :message_tree_id;
DELETE FROM message WHERE message_tree_id = :message_tree_id;
"""
        r = self.db.execute(text(sql_purge_message_tree), {"message_tree_id": message_tree_id})
        logger.debug(f"purge_message_tree({message_tree_id=}) {r.rowcount} rows.")

    @managed_tx_method(CommitMode.FLUSH)
    def purge_user_messages(
        self,
        user_id: UUID,
        purge_initial_prompts: bool = True,
        min_date: datetime = None,
        max_date: datetime = None,
    ):

        # find all affected message trees
        replies_by_tree, prompts = self.get_user_messages_by_tree(user_id, min_date, max_date)
        total_messages = sum(len(x) for x in replies_by_tree.values())
        logger.debug(f"found: {len(replies_by_tree)} trees; {len(prompts)} prompts; {total_messages} messages;")

        # remove all trees based on inital prompts of the user
        if purge_initial_prompts:
            for p in prompts:
                self.purge_message_tree(p.message_tree_id)
                if p.message_tree_id in replies_by_tree:
                    del replies_by_tree[p.message_tree_id]

        # patch all affected message trees
        for tree_id, replies in replies_by_tree.items():
            bad_parent_ids = set(m.id for m in replies)
            logger.debug(f"patching tree {tree_id=}, {bad_parent_ids=}")

            tree_messages = self.pr.fetch_message_tree(tree_id, reviewed=False, include_deleted=True)
            logger.debug(f"{tree_id=}, {len(bad_parent_ids)=}, {len(tree_messages)=}")
            by_id = {m.id: m for m in tree_messages}

            def ancestor_ids(msg: Message) -> list[UUID]:
                t = []
                while msg.parent_id is not None:
                    msg = by_id[msg.parent_id]
                    t.append(msg.id)
                return t

            def is_descendant_of_deleted(m: Message) -> bool:
                if m.id in bad_parent_ids:
                    return True
                ancestors = ancestor_ids(m)
                if any(a in bad_parent_ids for a in ancestors):
                    return True
                return False

            # start with deepest messages first
            tree_messages.sort(key=lambda x: x.depth, reverse=True)
            for m in tree_messages:
                if is_descendant_of_deleted(m):
                    logger.debug(f"purging message: {m.id}")
                    self._purge_message_internal(m.id)

            # update childern counts
            self.pr.update_children_counts(m.message_tree_id)

            # reactivate tree
            logger.info(f"reactivating message tree {tree_id}")
            mts = self.pr.fetch_tree_state(tree_id)
            mts.active = True
            self._enter_state(mts, message_tree_state.State.INITIAL_PROMPT_REVIEW)
            self.check_condition_for_growing_state(tree_id)
            self.check_condition_for_ranking_state(tree_id)
            self.check_condition_for_scoring_state(tree_id)

    @managed_tx_method(CommitMode.FLUSH)
    def purge_user(self, user_id: UUID, ban: bool = True) -> None:
        self.purge_user_messages(user_id, purge_initial_prompts=True)

        # delete all remaining rows and ban user
        sql_purge_user = """
DELETE FROM journal WHERE user_id = :user_id;
DELETE FROM message_reaction WHERE user_id = :user_id;
DELETE FROM message_emoji WHERE user_id = :user_id;
DELETE FROM task WHERE user_id = :user_id;
DELETE FROM message WHERE user_id = :user_id;
DELETE FROM user_stats WHERE user_id = :user_id;
"""

        r = self.db.execute(text(sql_purge_user), {"user_id": user_id})
        logger.debug(f"purge_user({user_id=}): {r.rowcount} rows.")

        if ban:
            self.db.execute(update(User).filter(User.id == user_id).values(deleted=True, enabled=False))

    def export_trees_to_file(
        self,
        message_tree_ids: list[str],
        file=None,
        reviewed: bool = True,
        include_deleted: bool = False,
        use_compression: bool = False,
    ) -> None:
        trees_to_export: List[tree_export.ExportMessageTree] = []

        for message_tree_id in message_tree_ids:
            messages: List[Message] = self.pr.fetch_message_tree(message_tree_id, reviewed, include_deleted)
            trees_to_export.append(tree_export.build_export_tree(message_tree_id, messages))

        if file:
            tree_export.write_trees_to_file(file, trees_to_export, use_compression)
        else:
            sys.stdout.write(json.dumps(jsonable_encoder(trees_to_export), indent=2))

    def export_all_ready_trees(
        self, file: str, reviewed: bool = True, include_deleted: bool = False, use_compression: bool = False
    ) -> None:
        message_tree_states: MessageTreeState = self.pr.fetch_message_trees_ready_for_export()
        message_tree_ids = [ms.message_tree_id for ms in message_tree_states]
        self.export_trees_to_file(message_tree_ids, file, reviewed, include_deleted, use_compression)

    def export_all_user_trees(
        self,
        user_id: str,
        file: str,
        reviewed: bool = True,
        include_deleted: bool = False,
        use_compression: bool = False,
    ) -> None:
        messages = self.pr.fetch_user_message_trees(UUID(user_id))
        message_tree_ids = [ms.message_tree_id for ms in messages]
        self.export_trees_to_file(message_tree_ids, file, reviewed, include_deleted, use_compression)

    @managed_tx_method(CommitMode.COMMIT)
    def retry_scoring_failed_message_trees(self):
        query = self.db.query(MessageTreeState).filter(
            MessageTreeState.state == message_tree_state.State.SCORING_FAILED
        )
        for mts in query.all():
            mts: MessageTreeState
            try:
                if not self.check_condition_for_scoring_state(mts.message_tree_id):
                    mts.active = True
                    self._enter_state(mts, message_tree_state.State.RANKING)
            except Exception:
                logger.exception(f"retry_scoring_failed_message_trees failed for ({mts.message_tree_id=})")


if __name__ == "__main__":
    from oasst_backend.api.deps import api_auth

    # from oasst_backend.api.deps import create_api_client
    from oasst_backend.database import engine
    from oasst_backend.prompt_repository import PromptRepository

    with Session(engine) as db:
        api_client = api_auth(settings.OFFICIAL_WEB_API_KEY, db=db)
        # api_client = create_api_client(session=db, description="test", frontend_type="bot")
        dummy_user = protocol_schema.User(id="__dummy_user__", display_name="Dummy User", auth_method="local")

        pr = PromptRepository(db=db, api_client=api_client, client_user=dummy_user)
        cfg = TreeManagerConfiguration()
        tm = TreeManager(db, pr, cfg)
        tm.ensure_tree_states()

        # tm.purge_user_messages(user_id=UUID("2ef9ad21-0dc5-442d-8750-6f7f1790723f"), purge_initial_prompts=False)
        # tm.purge_user(user_id=UUID("2ef9ad21-0dc5-442d-8750-6f7f1790723f"))
        # db.commit()

        # print("query_num_active_trees", tm.query_num_active_trees())
        # print("query_incomplete_rankings", tm.query_incomplete_rankings())
        # print("query_replies_need_review", tm.query_replies_need_review())
        # print("query_incomplete_reply_reviews", tm.query_replies_need_review())
        # print("query_incomplete_initial_prompt_reviews", tm.query_prompts_need_review())
        # print("query_extendible_trees", tm.query_extendible_trees())
        # print("query_extendible_parents", tm.query_extendible_parents())

        # print("next_task:", tm.next_task())

        print(
            ".query_tree_ranking_results", tm.query_tree_ranking_results(UUID("21f9d585-d22c-44ab-a696-baa3d83b5f1b"))
        )

        # print(tm.export_trees_to_file(message_tree_ids=["7e75fb38-e664-4e2b-817c-b9a0b01b0074"], file="lol.jsonl"))
