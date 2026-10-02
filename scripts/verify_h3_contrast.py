"""Actual pinned contrast-node schema and CPU tensor contract; no weights/jobs."""


def verify_contrast(nodes):
    import torch
    from comfy.nested_tensor import NestedTensor

    cls = nodes.NODE_CLASS_MAPPINGS["MiniMaxH3LatentContrast"]
    required = cls.INPUT_TYPES()["required"]
    assert set(required) == {"samples", "contrast", "preserve_norm"}
    assert required["samples"] == ("LATENT",)
    assert required["contrast"][0] == "FLOAT"
    assert {k: required["contrast"][1][k] for k in ("default", "min", "max")} == {
        "default": 1.0,
        "min": 0.0,
        "max": 3.0,
    }
    assert required["preserve_norm"][1]["default"] is True
    assert cls.RETURN_TYPES == ("LATENT",) and cls.FUNCTION == "contrast"
    node = cls()
    for dtype in (torch.float32, torch.float16, torch.bfloat16):
        video = torch.linspace(-2, 3, 2 * 24 * 3 * 4 * 4).reshape(2, 24, 3, 4, 4).to(dtype)
        audio = torch.ones(2, 32, 2, 5, dtype=dtype)
        before = video.clone()
        mask = NestedTensor((torch.ones_like(video), torch.zeros_like(audio)))
        latent = {"samples": NestedTensor((video, audio)), "noise_mask": mask, "tag": "retained"}
        assert node.contrast(latent, 1.0, True)[0] is latent
        for factor in (0.0, 0.8, 0.9, 3.0):
            result = node.contrast(latent, factor, True)[0]
            actual, sound = result["samples"].tensors
            assert actual.shape == video.shape and actual.dtype == dtype
            assert sound is audio and result["noise_mask"] is mask and result["tag"] == "retained"
            assert torch.equal(video, before) and torch.isfinite(actual).all()
            # Independent expression of the documented upstream operation.
            source = video.float()
            mean = source.mean(dim=(2, 3, 4), keepdim=True)
            expected = mean + factor * (source - mean)
            expected *= source.norm(dim=1, keepdim=True) / expected.norm(
                dim=1, keepdim=True
            ).clamp_min(1e-6)
            torch.testing.assert_close(actual, expected.to(dtype))
    print("Pinned H3 contrast schema/math/audio/no-op contract passed (CPU, no jobs).")
