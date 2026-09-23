"""Private, uncached CloudFront transport for existing S3 SigV4 grants.

The distribution MUST forward all credentials and disable caching on every
behavior. S3, not CloudFront, continues to authorize each individual request.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class PrivateDeliveryRoute:
    domain: str
    bucket: str
    region: str
    prefix: Literal["models", "assets"]

    def __post_init__(self) -> None:
        if re.fullmatch(r"d[a-z0-9]{1,62}\.cloudfront\.net", self.domain) is None:
            raise ValueError("private delivery requires a CloudFront distribution hostname")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket) is None:
            raise ValueError("private delivery requires a DNS-compatible, non-dotted S3 bucket")
        if re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", self.region) is None:
            raise ValueError("private delivery requires a commercial regional S3 origin")
        if self.prefix not in {"models", "assets"}:
            raise ValueError("invalid private delivery route")

    @property
    def origin(self) -> str:
        return f"{self.bucket}.s3.{self.region}.amazonaws.com"

    def rewrite(self, url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != self.origin
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            # Never include the URL: it may contain temporary credentials.
            raise ValueError("private delivery received an unexpected S3 origin")
        # Preserve encoded path/query bytes exactly. The viewer function removes
        # this prefix and the origin policy restores the signed regional Host.
        return urlunsplit(("https", self.domain, f"/{self.prefix}{parsed.path}", parsed.query, ""))

    def before_send(self, request: Any, **_: Any) -> None:
        """Run AFTER botocore signing, only for GetObject on a dedicated client."""
        request.url = self.rewrite(request.url)
        request.headers["Host"] = self.domain
