"""Native Comfy 0.37 choices, checked against real imports by the image build."""

H3_SAMPLERS = (
    "euler",
    "euler_cfg_pp",
    "euler_ancestral",
    "euler_ancestral_cfg_pp",
    "heun",
    "heunpp2",
    "exp_heun_2_x0",
    "exp_heun_2_x0_sde",
    "dpm_2",
    "dpm_2_ancestral",
    "lms",
    "dpm_fast",
    "dpm_adaptive",
    "dpmpp_2s_ancestral",
    "dpmpp_2s_ancestral_cfg_pp",
    "dpmpp_sde",
    "dpmpp_sde_gpu",
    "dpmpp_2m",
    "dpmpp_2m_cfg_pp",
    "dpmpp_2m_sde",
    "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun",
    "dpmpp_2m_sde_heun_gpu",
    "dpmpp_3m_sde",
    "dpmpp_3m_sde_gpu",
    "ddpm",
    "lcm",
    "ipndm",
    "ipndm_v",
    "deis",
    "cfgpp_ud10_ab",
    "res_multistep",
    "res_multistep_cfg_pp",
    "res_multistep_ancestral",
    "res_multistep_ancestral_cfg_pp",
    "gradient_estimation",
    "gradient_estimation_cfg_pp",
    "er_sde",
    "seeds_2",
    "seeds_3",
    "sa_solver",
    "sa_solver_pece",
    "ddim",
    "uni_pc",
    "uni_pc_bh2",
)
H3_SCHEDULERS = (
    "simple",
    "sgm_uniform",
    "karras",
    "exponential",
    "ddim_uniform",
    "beta",
    "normal",
    "linear_quadratic",
    "kl_optimal",
)
# Eros' published style-preservation recipe. beta57 is NOT stock beta:
# RES4LYF uses native beta_scheduler(alpha=0.5, beta=0.7). We compose the
# installed native BetaSamplingScheduler/SplitSigmas nodes; no plugin patch.
H3_EROS_SCHEDULERS = (*H3_SCHEDULERS, "beta57")

# Omit these unchanged defaults from payloads sent during control-plane-first
# rollout. Old strict workers must continue accepting their existing Turbo jobs.
H3_EXPERT_DEFAULTS = {
    "h3_denoise": 1.0,
    "h3_refine_steps": 1,
    "h3_refine_cfg": 1.0,
    "h3_refine_denoise": 0.2,
    "h3_refine_sampler": None,
    "h3_refine_scheduler": "simple",
}
