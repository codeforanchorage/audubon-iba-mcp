"""Pydantic configuration schema for the Audubon IBA plugin."""

from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AudubonIBAPluginConfig(BaseModel):
    """Configuration for the Audubon Important Bird Areas plugin.

    This plugin fronts a single, fixed ArcGIS FeatureServer with a known
    schema -- there is no catalog to search and no dataset to resolve, so
    the only knobs are the service URL, the polygon layer id, and HTTP
    behaviour.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    service_url: str = Field(
        ...,
        description=(
            "IBA FeatureServer root, e.g. https://services1.arcgis.com/"
            "lDFzr3JyGEn5Eymu/arcgis/rest/services/iba_polygons_public/FeatureServer"
        ),
    )
    layer_id: int = Field(
        default=0,
        ge=0,
        description="Layer id of the IBA polygon layer within the service",
    )
    organization: str = Field(
        default="National Audubon Society",
        description="Publisher of the service, shown in tool output",
    )
    timeout: int = Field(
        default=120, ge=1, le=300, description="HTTP request timeout in seconds"
    )
    token: Optional[str] = Field(
        None,
        description=(
            "Optional ArcGIS token. The public IBA service needs none; this "
            "exists only so the same plugin can front a secured mirror."
        ),
    )

    @field_validator("service_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate that the service URL is well-formed."""
        if not v:
            raise ValueError("URL cannot be empty")
        result = urlparse(v)
        if not result.scheme or not result.netloc:
            raise ValueError("URL must include scheme (http/https) and hostname")
        if result.scheme not in ("http", "https"):
            raise ValueError("URL scheme must be http or https")
        return v.rstrip("/")

    model_config = ConfigDict(extra="forbid")
