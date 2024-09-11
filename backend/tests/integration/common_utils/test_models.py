from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

from danswer.auth.schemas import UserRole
from danswer.search.enums import RecencyBiasSetting
from danswer.server.documents.models import DocumentSource
from danswer.server.documents.models import InputType

"""
These data models are used to represent the data on the testing side of things.
This means the flow is:
1. Make request that changes data in db
2. Make a change to the testing model
3. Retrieve data from db
4. Compare db data with testing model to verify
"""


class TestAPIKey(BaseModel):
    api_key_id: int
    api_key_display: str
    api_key: str | None = None  # only present on initial creation
    api_key_name: str | None = None
    api_key_role: UserRole

    user_id: UUID
    headers: dict


class TestUser(BaseModel):
    id: str
    email: str
    password: str
    headers: dict


class TestCredential(BaseModel):
    id: int
    name: str
    credential_json: dict[str, Any]
    admin_public: bool
    source: DocumentSource
    curator_public: bool
    groups: list[int]


class TestConnector(BaseModel):
    id: int
    name: str
    source: DocumentSource
    input_type: InputType
    connector_specific_config: dict[str, Any]
    groups: list[int] | None = None
    is_public: bool | None = None


class SimpleTestDocument(BaseModel):
    id: str
    content: str


class TestCCPair(BaseModel):
    id: int
    name: str
    connector_id: int
    credential_id: int
    is_public: bool
    groups: list[int]
    documents: list[SimpleTestDocument] = Field(default_factory=list)


class TestUserGroup(BaseModel):
    id: int
    name: str
    user_ids: list[str]
    cc_pair_ids: list[int]


class TestLLMProvider(BaseModel):
    id: int
    name: str
    provider: str
    api_key: str
    default_model_name: str
    is_public: bool
    groups: list[TestUserGroup]
    api_base: str | None = None
    api_version: str | None = None


class TestDocumentSet(BaseModel):
    id: int
    name: str
    description: str
    cc_pair_ids: list[int] = Field(default_factory=list)
    is_public: bool
    is_up_to_date: bool
    users: list[str] = Field(default_factory=list)
    groups: list[int] = Field(default_factory=list)


class TestPersona(BaseModel):
    id: int
    name: str
    description: str
    num_chunks: float
    llm_relevance_filter: bool
    is_public: bool
    llm_filter_extraction: bool
    recency_bias: RecencyBiasSetting
    prompt_ids: list[int]
    document_set_ids: list[int]
    tool_ids: list[int]
    llm_model_provider_override: str | None
    llm_model_version_override: str | None
    users: list[str]
    groups: list[int]
