import io
import os
from collections.abc import Callable
from collections.abc import Collection
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from typing import Any
from typing import cast

import bs4
from atlassian import Confluence  # type:ignore
from requests import HTTPError

from danswer.configs.app_configs import (
    CONFLUENCE_CONNECTOR_ATTACHMENT_CHAR_COUNT_THRESHOLD,
)
from danswer.configs.app_configs import CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD
from danswer.configs.app_configs import CONFLUENCE_CONNECTOR_INDEX_ARCHIVED_PAGES
from danswer.configs.app_configs import CONFLUENCE_CONNECTOR_LABELS_TO_SKIP
from danswer.configs.app_configs import CONFLUENCE_CONNECTOR_SKIP_LABEL_INDEXING
from danswer.configs.app_configs import CONTINUE_ON_CONNECTOR_FAILURE
from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.confluence.confluence_utils import (
    build_confluence_document_id,
)
from danswer.connectors.confluence.confluence_utils import get_used_attachments
from danswer.connectors.confluence.rate_limit_handler import (
    make_confluence_call_handle_rate_limit,
)
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import BasicExpertInfo
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.file_processing.extract_file_text import extract_file_text
from danswer.file_processing.html_utils import format_document_soup
from danswer.utils.logger import setup_logger

logger = setup_logger()

# Potential Improvements
# 1. Include attachments, etc
# 2. Segment into Sections for more accurate linking, can split by headers but make sure no text/ordering is lost


NO_PERMISSIONS_TO_VIEW_ATTACHMENTS_ERROR_STR = (
    "User not permitted to view attachments on content"
)
NO_PARENT_OR_NO_PERMISSIONS_ERROR_STR = (
    "No parent or not permitted to view content with id"
)


@lru_cache()
def _get_user(user_id: str, confluence_client: Confluence) -> str:
    """Get Confluence Display Name based on the account-id or userkey value

    Args:
        user_id (str): The user id (i.e: the account-id or userkey)
        confluence_client (Confluence): The Confluence Client

    Returns:
        str: The User Display Name. 'Unknown User' if the user is deactivated or not found
    """
    user_not_found = "Unknown User"

    get_user_details_by_accountid = make_confluence_call_handle_rate_limit(
        confluence_client.get_user_details_by_accountid
    )
    try:
        return get_user_details_by_accountid(user_id).get("displayName", user_not_found)
    except Exception as e:
        logger.warning(
            f"Unable to get the User Display Name with the id: '{user_id}' - {e}"
        )
    return user_not_found


def parse_html_page(text: str, confluence_client: Confluence) -> str:
    """Parse a Confluence html page and replace the 'user Id' by the real
        User Display Name

    Args:
        text (str): The page content
        confluence_client (Confluence): Confluence client

    Returns:
        str: loaded and formated Confluence page
    """
    soup = bs4.BeautifulSoup(text, "html.parser")
    for user in soup.findAll("ri:user"):
        user_id = (
            user.attrs["ri:account-id"]
            if "ri:account-id" in user.attrs
            else user.get("ri:userkey")
        )
        if not user_id:
            logger.warning(
                "ri:userkey not found in ri:user element. " f"Found attrs: {user.attrs}"
            )
            continue
        # Include @ sign for tagging, more clear for LLM
        user.replaceWith("@" + _get_user(user_id, confluence_client))
    return format_document_soup(soup)


def _comment_dfs(
    comments_str: str,
    comment_pages: Collection[dict[str, Any]],
    confluence_client: Confluence,
) -> str:
    get_page_child_by_type = make_confluence_call_handle_rate_limit(
        confluence_client.get_page_child_by_type
    )

    for comment_page in comment_pages:
        comment_html = comment_page["body"]["storage"]["value"]
        comments_str += "\nComment:\n" + parse_html_page(
            comment_html, confluence_client
        )
        try:
            child_comment_pages = get_page_child_by_type(
                comment_page["id"],
                type="comment",
                start=None,
                limit=None,
                expand="body.storage.value",
            )
            comments_str = _comment_dfs(
                comments_str, child_comment_pages, confluence_client
            )
        except HTTPError as e:
            # not the cleanest, but I'm not aware of a nicer way to check the error
            if NO_PARENT_OR_NO_PERMISSIONS_ERROR_STR not in str(e):
                raise

    return comments_str


def _datetime_from_string(datetime_string: str) -> datetime:
    datetime_object = datetime.fromisoformat(datetime_string)

    if datetime_object.tzinfo is None:
        # If no timezone info, assume it is UTC
        datetime_object = datetime_object.replace(tzinfo=timezone.utc)
    else:
        # If not in UTC, translate it
        datetime_object = datetime_object.astimezone(timezone.utc)

    return datetime_object


class RecursiveIndexer:
    def __init__(
        self,
        batch_size: int,
        confluence_client: Confluence,
        index_recursively: bool,
        origin_page_id: str,
    ) -> None:
        self.batch_size = 1
        # batch_size
        self.confluence_client = confluence_client
        self.index_recursively = index_recursively
        self.origin_page_id = origin_page_id
        self.pages = self.recurse_children_pages(0, self.origin_page_id)

    def get_origin_page(self) -> list[dict[str, Any]]:
        return [self._fetch_origin_page()]

    def get_pages(self, ind: int, size: int) -> list[dict]:
        if ind * size > len(self.pages):
            return []
        return self.pages[ind * size : (ind + 1) * size]

    def _fetch_origin_page(
        self,
    ) -> dict[str, Any]:
        get_page_by_id = make_confluence_call_handle_rate_limit(
            self.confluence_client.get_page_by_id
        )
        try:
            origin_page = get_page_by_id(
                self.origin_page_id, expand="body.storage.value,version"
            )
            return origin_page
        except Exception as e:
            logger.warning(
                f"Appending orgin page with id {self.origin_page_id} failed: {e}"
            )
            return {}

    def recurse_children_pages(
        self,
        start_ind: int,
        page_id: str,
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        current_level_pages: list[dict[str, Any]] = []
        next_level_pages: list[dict[str, Any]] = []

        # Initial fetch of first level children
        index = start_ind
        while batch := self._fetch_single_depth_child_pages(
            index, self.batch_size, page_id
        ):
            current_level_pages.extend(batch)
            index += len(batch)

        pages.extend(current_level_pages)

        # Recursively index children and children's children, etc.
        while current_level_pages:
            for child in current_level_pages:
                child_index = 0
                while child_batch := self._fetch_single_depth_child_pages(
                    child_index, self.batch_size, child["id"]
                ):
                    next_level_pages.extend(child_batch)
                    child_index += len(child_batch)

            pages.extend(next_level_pages)
            current_level_pages = next_level_pages
            next_level_pages = []

        try:
            origin_page = self._fetch_origin_page()
            pages.append(origin_page)
        except Exception as e:
            logger.warning(f"Appending origin page with id {page_id} failed: {e}")

        return pages

    def _fetch_single_depth_child_pages(
        self, start_ind: int, batch_size: int, page_id: str
    ) -> list[dict[str, Any]]:
        child_pages: list[dict[str, Any]] = []

        get_page_child_by_type = make_confluence_call_handle_rate_limit(
            self.confluence_client.get_page_child_by_type
        )

        try:
            child_page = get_page_child_by_type(
                page_id,
                type="page",
                start=start_ind,
                limit=batch_size,
                expand="body.storage.value,version",
            )

            child_pages.extend(child_page)
            return child_pages

        except Exception:
            logger.warning(
                f"Batch failed with page {page_id} at offset {start_ind} "
                f"with size {batch_size}, processing pages individually..."
            )

            for i in range(batch_size):
                ind = start_ind + i
                try:
                    child_page = get_page_child_by_type(
                        page_id,
                        type="page",
                        start=ind,
                        limit=1,
                        expand="body.storage.value,version",
                    )
                    child_pages.extend(child_page)
                except Exception as e:
                    logger.warning(f"Page {page_id} at offset {ind} failed: {e}")
                    raise e

            return child_pages


class ConfluenceConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        wiki_base: str,
        space: str,
        is_cloud: bool,
        page_id: str = "",
        index_recursively: bool = True,
        batch_size: int = INDEX_BATCH_SIZE,
        continue_on_failure: bool = CONTINUE_ON_CONNECTOR_FAILURE,
        # if a page has one of the labels specified in this list, we will just
        # skip it. This is generally used to avoid indexing extra sensitive
        # pages.
        labels_to_skip: list[str] = CONFLUENCE_CONNECTOR_LABELS_TO_SKIP,
    ) -> None:
        self.batch_size = batch_size
        self.continue_on_failure = continue_on_failure
        self.labels_to_skip = set(labels_to_skip)
        self.recursive_indexer: RecursiveIndexer | None = None
        self.index_recursively = index_recursively

        # Remove trailing slash from wiki_base if present
        self.wiki_base = wiki_base.rstrip("/")
        self.space = space
        self.page_id = page_id

        self.is_cloud = is_cloud

        self.space_level_scan = False
        self.confluence_client: Confluence | None = None

        if self.page_id is None or self.page_id == "":
            self.space_level_scan = True

        logger.info(
            f"wiki_base: {self.wiki_base}, space: {self.space}, page_id: {self.page_id},"
            + f" space_level_scan: {self.space_level_scan}, index_recursively: {self.index_recursively}"
        )

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        username = credentials["confluence_username"]
        access_token = credentials["confluence_access_token"]
        self.confluence_client = Confluence(
            url=self.wiki_base,
            # passing in username causes issues for Confluence data center
            username=username if self.is_cloud else None,
            password=access_token if self.is_cloud else None,
            token=access_token if not self.is_cloud else None,
        )
        return None

    def _fetch_pages(
        self,
        confluence_client: Confluence,
        start_ind: int,
    ) -> list[dict[str, Any]]:
        def _fetch_space(start_ind: int, batch_size: int) -> list[dict[str, Any]]:
            get_all_pages_from_space = make_confluence_call_handle_rate_limit(
                confluence_client.get_all_pages_from_space
            )
            try:
                return get_all_pages_from_space(
                    self.space,
                    start=start_ind,
                    limit=batch_size,
                    status=(
                        None if CONFLUENCE_CONNECTOR_INDEX_ARCHIVED_PAGES else "current"
                    ),
                    expand="body.storage.value,version",
                )
            except Exception:
                logger.warning(
                    f"Batch failed with space {self.space} at offset {start_ind} "
                    f"with size {batch_size}, processing pages individually..."
                )

                view_pages: list[dict[str, Any]] = []
                for i in range(self.batch_size):
                    try:
                        # Could be that one of the pages here failed due to this bug:
                        # https://jira.atlassian.com/browse/CONFCLOUD-76433
                        view_pages.extend(
                            get_all_pages_from_space(
                                self.space,
                                start=start_ind + i,
                                limit=1,
                                status=(
                                    None
                                    if CONFLUENCE_CONNECTOR_INDEX_ARCHIVED_PAGES
                                    else "current"
                                ),
                                expand="body.storage.value,version",
                            )
                        )
                    except HTTPError as e:
                        logger.warning(
                            f"Page failed with space {self.space} at offset {start_ind + i}, "
                            f"trying alternative expand option: {e}"
                        )
                        # Use view instead, which captures most info but is less complete
                        view_pages.extend(
                            get_all_pages_from_space(
                                self.space,
                                start=start_ind + i,
                                limit=1,
                                expand="body.view.value,version",
                            )
                        )

                return view_pages

        def _fetch_page(start_ind: int, batch_size: int) -> list[dict[str, Any]]:
            if self.recursive_indexer is None:
                self.recursive_indexer = RecursiveIndexer(
                    origin_page_id=self.page_id,
                    batch_size=self.batch_size,
                    confluence_client=self.confluence_client,
                    index_recursively=self.index_recursively,
                )

            if self.index_recursively:
                return self.recursive_indexer.get_pages(start_ind, batch_size)
            else:
                return self.recursive_indexer.get_origin_page()

        pages: list[dict[str, Any]] = []

        try:
            pages = (
                _fetch_space(start_ind, self.batch_size)
                if self.space_level_scan
                else _fetch_page(start_ind, self.batch_size)
            )
            return pages

        except Exception as e:
            if not self.continue_on_failure:
                raise e

        # error checking phase, only reachable if `self.continue_on_failure=True`
        for i in range(self.batch_size):
            try:
                pages = (
                    _fetch_space(start_ind, self.batch_size)
                    if self.space_level_scan
                    else _fetch_page(start_ind, self.batch_size)
                )
                return pages

            except Exception:
                logger.exception(
                    "Ran into exception when fetching pages from Confluence"
                )

        return pages

    def _fetch_comments(self, confluence_client: Confluence, page_id: str) -> str:
        get_page_child_by_type = make_confluence_call_handle_rate_limit(
            confluence_client.get_page_child_by_type
        )

        try:
            comment_pages = cast(
                Collection[dict[str, Any]],
                get_page_child_by_type(
                    page_id,
                    type="comment",
                    start=None,
                    limit=None,
                    expand="body.storage.value",
                ),
            )
            return _comment_dfs("", comment_pages, confluence_client)
        except Exception as e:
            if not self.continue_on_failure:
                raise e

            logger.exception(
                "Ran into exception when fetching comments from Confluence"
            )
            return ""

    def _fetch_labels(self, confluence_client: Confluence, page_id: str) -> list[str]:
        get_page_labels = make_confluence_call_handle_rate_limit(
            confluence_client.get_page_labels
        )
        try:
            labels_response = get_page_labels(page_id)
            return [label["name"] for label in labels_response["results"]]
        except Exception as e:
            if not self.continue_on_failure:
                raise e

            logger.exception("Ran into exception when fetching labels from Confluence")
            return []

    @classmethod
    def _attachment_to_download_link(
        cls, confluence_client: Confluence, attachment: dict[str, Any]
    ) -> str:
        return confluence_client.url + attachment["_links"]["download"]

    @classmethod
    def _attachment_to_content(
        cls,
        confluence_client: Confluence,
        attachment: dict[str, Any],
    ) -> str | None:
        """If it returns None, assume that we should skip this attachment."""
        if attachment["metadata"]["mediaType"] in [
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/svg+xml",
            "video/mp4",
            "video/quicktime",
        ]:
            return None

        download_link = cls._attachment_to_download_link(confluence_client, attachment)

        attachment_size = attachment["extensions"]["fileSize"]
        if attachment_size > CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD:
            logger.warning(
                f"Skipping {download_link} due to size. "
                f"size={attachment_size} "
                f"threshold={CONFLUENCE_CONNECTOR_ATTACHMENT_SIZE_THRESHOLD}"
            )
            return None

        response = confluence_client._session.get(download_link)
        if response.status_code != 200:
            logger.warning(
                f"Failed to fetch {download_link} with invalid status code {response.status_code}"
            )
            return None

        extracted_text = extract_file_text(
            attachment["title"], io.BytesIO(response.content), False
        )
        if len(extracted_text) > CONFLUENCE_CONNECTOR_ATTACHMENT_CHAR_COUNT_THRESHOLD:
            logger.warning(
                f"Skipping {download_link} due to char count. "
                f"char count={len(extracted_text)} "
                f"threshold={CONFLUENCE_CONNECTOR_ATTACHMENT_CHAR_COUNT_THRESHOLD}"
            )
            return None

        return extracted_text

    def _fetch_attachments(
        self, confluence_client: Confluence, page_id: str, files_in_used: list[str]
    ) -> tuple[str, list[dict[str, Any]]]:
        unused_attachments: list = []

        get_attachments_from_content = make_confluence_call_handle_rate_limit(
            confluence_client.get_attachments_from_content
        )
        files_attachment_content: list = []

        try:
            expand = "history.lastUpdated,metadata.labels"
            attachments_container = get_attachments_from_content(
                page_id, start=0, limit=500, expand=expand
            )
            for attachment in attachments_container["results"]:
                if attachment["title"] not in files_in_used:
                    unused_attachments.append(attachment)
                    continue

                attachment_content = self._attachment_to_content(
                    confluence_client, attachment
                )
                if attachment_content:
                    files_attachment_content.append(attachment_content)

        except Exception as e:
            if isinstance(
                e, HTTPError
            ) and NO_PERMISSIONS_TO_VIEW_ATTACHMENTS_ERROR_STR in str(e):
                logger.warning(
                    f"User does not have access to attachments on page '{page_id}'"
                )
                return "", []

            if not self.continue_on_failure:
                raise e
            logger.exception(
                f"Ran into exception when fetching attachments from Confluence: {e}"
            )

        return "\n".join(files_attachment_content), unused_attachments

    def _get_doc_batch(
        self, start_ind: int, time_filter: Callable[[datetime], bool] | None = None
    ) -> tuple[list[Document], list[dict[str, Any]], int]:
        doc_batch: list[Document] = []
        unused_attachments: list[dict[str, Any]] = []

        if self.confluence_client is None:
            raise ConnectorMissingCredentialError("Confluence")
        batch = self._fetch_pages(self.confluence_client, start_ind)

        for page in batch:
            last_modified = _datetime_from_string(page["version"]["when"])
            author = cast(str | None, page["version"].get("by", {}).get("email"))

            if time_filter and not time_filter(last_modified):
                continue

            page_id = page["id"]

            if self.labels_to_skip or not CONFLUENCE_CONNECTOR_SKIP_LABEL_INDEXING:
                page_labels = self._fetch_labels(self.confluence_client, page_id)

            # check disallowed labels
            if self.labels_to_skip:
                label_intersection = self.labels_to_skip.intersection(page_labels)
                if label_intersection:
                    logger.info(
                        f"Page with ID '{page_id}' has a label which has been "
                        f"designated as disallowed: {label_intersection}. Skipping."
                    )

                    continue

            page_html = (
                page["body"].get("storage", page["body"].get("view", {})).get("value")
            )
            # The url and the id are the same
            page_url = build_confluence_document_id(
                self.wiki_base, page["_links"]["webui"]
            )
            if not page_html:
                logger.debug("Page is empty, skipping: %s", page_url)
                continue
            page_text = parse_html_page(page_html, self.confluence_client)

            files_in_used = get_used_attachments(page_html)
            attachment_text, unused_page_attachments = self._fetch_attachments(
                self.confluence_client, page_id, files_in_used
            )
            unused_attachments.extend(unused_page_attachments)

            page_text += attachment_text
            comments_text = self._fetch_comments(self.confluence_client, page_id)
            page_text += comments_text
            doc_metadata: dict[str, str | list[str]] = {"Wiki Space Name": self.space}
            if not CONFLUENCE_CONNECTOR_SKIP_LABEL_INDEXING and page_labels:
                doc_metadata["labels"] = page_labels

            doc_batch.append(
                Document(
                    id=page_url,
                    sections=[Section(link=page_url, text=page_text)],
                    source=DocumentSource.CONFLUENCE,
                    semantic_identifier=page["title"],
                    doc_updated_at=last_modified,
                    primary_owners=(
                        [BasicExpertInfo(email=author)] if author else None
                    ),
                    metadata=doc_metadata,
                )
            )
        return (
            doc_batch,
            unused_attachments,
            len(batch),
        )

    def _get_attachment_batch(
        self,
        start_ind: int,
        attachments: list[dict[str, Any]],
        time_filter: Callable[[datetime], bool] | None = None,
    ) -> tuple[list[Document], int]:
        doc_batch: list[Document] = []

        if self.confluence_client is None:
            raise ConnectorMissingCredentialError("Confluence")

        end_ind = min(start_ind + self.batch_size, len(attachments))

        for attachment in attachments[start_ind:end_ind]:
            last_updated = _datetime_from_string(
                attachment["history"]["lastUpdated"]["when"]
            )

            if time_filter and not time_filter(last_updated):
                continue

            # The url and the id are the same
            attachment_url = build_confluence_document_id(
                self.wiki_base, attachment["_links"]["download"]
            )
            attachment_content = self._attachment_to_content(
                self.confluence_client, attachment
            )
            if attachment_content is None:
                continue

            creator_email = attachment["history"]["createdBy"].get("email")

            comment = attachment["metadata"].get("comment", "")
            doc_metadata: dict[str, str | list[str]] = {"comment": comment}

            attachment_labels: list[str] = []
            if not CONFLUENCE_CONNECTOR_SKIP_LABEL_INDEXING:
                for label in attachment["metadata"]["labels"]["results"]:
                    attachment_labels.append(label["name"])

            doc_metadata["labels"] = attachment_labels

            doc_batch.append(
                Document(
                    id=attachment_url,
                    sections=[Section(link=attachment_url, text=attachment_content)],
                    source=DocumentSource.CONFLUENCE,
                    semantic_identifier=attachment["title"],
                    doc_updated_at=last_updated,
                    primary_owners=(
                        [BasicExpertInfo(email=creator_email)]
                        if creator_email
                        else None
                    ),
                    metadata=doc_metadata,
                )
            )

        return doc_batch, end_ind - start_ind

    def load_from_state(self) -> GenerateDocumentsOutput:
        unused_attachments = []

        if self.confluence_client is None:
            raise ConnectorMissingCredentialError("Confluence")

        start_ind = 0
        while True:
            doc_batch, unused_attachments_batch, num_pages = self._get_doc_batch(
                start_ind
            )
            unused_attachments.extend(unused_attachments_batch)
            start_ind += num_pages
            if doc_batch:
                yield doc_batch

            if num_pages < self.batch_size:
                break

        start_ind = 0
        while True:
            attachment_batch, num_attachments = self._get_attachment_batch(
                start_ind, unused_attachments
            )
            start_ind += num_attachments
            if attachment_batch:
                yield attachment_batch

            if num_attachments < self.batch_size:
                break

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        unused_attachments = []

        if self.confluence_client is None:
            raise ConnectorMissingCredentialError("Confluence")

        start_time = datetime.fromtimestamp(start, tz=timezone.utc)
        end_time = datetime.fromtimestamp(end, tz=timezone.utc)

        start_ind = 0
        while True:
            doc_batch, unused_attachments_batch, num_pages = self._get_doc_batch(
                start_ind, time_filter=lambda t: start_time <= t <= end_time
            )
            unused_attachments.extend(unused_attachments_batch)

            start_ind += num_pages
            if doc_batch:
                yield doc_batch

            if num_pages < self.batch_size:
                break

        start_ind = 0
        while True:
            attachment_batch, num_attachments = self._get_attachment_batch(
                start_ind,
                unused_attachments,
                time_filter=lambda t: start_time <= t <= end_time,
            )
            start_ind += num_attachments
            if attachment_batch:
                yield attachment_batch

            if num_attachments < self.batch_size:
                break


if __name__ == "__main__":
    connector = ConfluenceConnector(
        wiki_base=os.environ["CONFLUENCE_TEST_SPACE_URL"],
        space=os.environ["CONFLUENCE_TEST_SPACE"],
        is_cloud=os.environ.get("CONFLUENCE_IS_CLOUD", "true").lower() == "true",
        page_id=os.environ.get("CONFLUENCE_TEST_PAGE_ID", ""),
        index_recursively=True,
    )
    connector.load_credentials(
        {
            "confluence_username": os.environ["CONFLUENCE_USER_NAME"],
            "confluence_access_token": os.environ["CONFLUENCE_ACCESS_TOKEN"],
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
