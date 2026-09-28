"""Output-only diagnostic: all inference nodes come from the author packages."""

from typing import Any


class ManagedH3DiagnosticDecode:
    """Decode the base only AFTER refinement, without another sampler execution."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:  # noqa: N802
        return {
            "required": {
                "samples": ("LATENT",),
                "vae": ("VAE",),
                "after_refinement": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "decode"
    CATEGORY = "GenAutomation/H3"

    def decode(self, samples: Any, vae: Any, after_refinement: Any) -> Any:
        from nodes import VAEDecode  # type: ignore[import-not-found]

        # The graph dependency deliberately prevents the extra VAE decode from
        # changing model residency during the sampling/refinement comparison.
        return VAEDecode().decode(vae, samples)


NODE_CLASS_MAPPINGS = {
    "ManagedH3DiagnosticDecode": ManagedH3DiagnosticDecode,
}
