import json
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

#
# Enum definitions
#


class OpenAPISchemaType(StrEnum):
    OBJECT = "object"
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    # Typically used to indicate an optional type within an anyOf statement
    NULL = "null"


class ParameterLocationType(StrEnum):
    PATH = "path"
    QUERY = "query"


class ActionType(StrEnum):
    GET = "get"
    POST = "post"
    PUT = "put"
    PATCH = "patch"
    DELETE = "delete"


#
# Nested schemas inside OpenAPI definitions
#


class OpenAPIProperty(BaseModel):
    """
    A property is the core wrapper for OpenAPI model objects. It allows users to recursively
    define data structures based on their type. Each property can have a list of subproperties, alongside
    the constraints of how they're used (ie. required attributes, union of attributes, etc).

    """

    title: str | None = None
    description: str | None = None
    properties: dict[str, "OpenAPIProperty"] = {}
    additionalProperties: Optional["OpenAPIProperty"] = None
    required: list[str] = []

    # Self-contained type: object, int, etc
    variable_type: OpenAPISchemaType | None = Field(alias="type", default=None)
    # Reference to another type
    ref: str | None = Field(alias="$ref", default=None)
    # Array of another type
    items: Optional["OpenAPIProperty"] = None
    # Pointer to multiple possible subtypes
    anyOf: list["OpenAPIProperty"] = []

    # Validator to ensure that one of the optional values is set
    @model_validator(mode="after")
    def check_provided_value(self) -> "OpenAPIProperty":
        if not any([self.variable_type, self.ref, self.items, self.anyOf]):
            raise ValueError("One of variable_type, $ref, or items must be set")
        return self

    def __hash__(self):
        # Normally we would make use of a frozen BaseClass to enable hashing, but since
        # dictionaries are included in the payload here an easier way is just to convert
        # to a JSON string and hash that.
        # We make sure to order the strings since otherwise the hash risks being different
        # despite having the same values
        def sort_json(obj):
            if isinstance(obj, dict):
                return sorted((k, sort_json(v)) for k, v in obj.items())
            else:
                return obj

        return hash(json.dumps(sort_json(self.model_dump())))


class ContentDefinition(BaseModel):
    class Reference(BaseModel):
        ref: str = Field(alias="$ref")

    schema_ref: Reference = Field(alias="schema")


class ContentBodyDefinition(BaseModel):
    # original key is a `content: { content_type: {schema: SchemaDefinition }}`
    content_type: str
    content_schema: ContentDefinition

    # Requests will typically provider their required status, requests will not.
    # Default these to True since they are required within the scope of the request
    # that is provided.
    required: bool = True

    @model_validator(mode="before")
    def explode_content_dictionary(cls, data: Any) -> Any:
        if "content" not in data or not isinstance(data["content"], dict):
            raise ValueError("RequestBodyDefinition.content must be a dict")

        # We only support a single content type for now
        if len(data["content"]) != 1:
            raise ValueError(
                "RequestBodyDefinition.content must have a single content type"
            )

        data["content_type"], data["content_schema"] = list(data["content"].items())[0]
        return data


class URLParameterDefinition(BaseModel):
    class Schema(BaseModel):
        type: OpenAPISchemaType
        title: str

        # Specified in the case of a known format that can be validated on the client-side, like a UUID
        format: str | None = None

    name: str
    in_location: ParameterLocationType = Field(alias="in")
    schema_ref: Schema = Field(alias="schema")
    required: bool


class ActionDefinition(BaseModel):
    action_type: ActionType

    summary: str
    operationId: str
    # Parameters injected into the URL path
    parameters: list[URLParameterDefinition] = []

    # { status_code: ResponseDefinition }
    responses: dict[str, ContentBodyDefinition]
    requestBody: ContentBodyDefinition | None = None


class EndpointDefinition(BaseModel):
    actions: list[ActionDefinition] = []

    @model_validator(mode="before")
    def inject_action_type(cls, data: Any) -> dict[str, Any]:
        """
        OpenAPI often defines metadata in a dict structure where the key is relevant
        to the behavior of the values. In our pipeline we want to be able to pass
        around ActionDefinitions to fully generate a resulting type action. We migrate
        the key-metadata into the actual definition itself.

        """
        if not isinstance(data, dict):
            raise ValueError("EndpointDefinition must be a dict")

        for action_type, payload in data.items():
            payload["action_type"] = action_type

        return {"actions": data.values()}


#
# Top-level OpenAPI definition entrypoints. These parse the high level spec.
#


class OpenAPISchema(OpenAPIProperty):
    """
    Defines the expected format for model-only definition schemas. This
    is the output when Pydantic is called with `model_json_schema`.

    """

    defs: dict[str, OpenAPIProperty] = Field(alias="$defs", default_factory=dict)


class OpenAPIDefinition(BaseModel):
    """
    Defines the spec for a whole OpenAPI API definition. This mirrors what FastAPI
    outputs as the /openapi.json endpoint.

    """

    class Components(BaseModel):
        schemas: dict[str, OpenAPIProperty]

    # { path: { action: ActionDefinition }}
    paths: dict[str, EndpointDefinition]
    components: Components
