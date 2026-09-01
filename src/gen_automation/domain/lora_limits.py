from gen_automation.domain.enums import GenerationModelFamily

MAX_ILLUSTRIOUS_LORAS = 8
MAX_ANIMA_LORAS = 16
MAX_GENERATION_LORAS = max(MAX_ILLUSTRIOUS_LORAS, MAX_ANIMA_LORAS)


def max_loras_for_model_family(model_family: GenerationModelFamily) -> int:
    if model_family == GenerationModelFamily.ANIMA:
        return MAX_ANIMA_LORAS
    return MAX_ILLUSTRIOUS_LORAS
