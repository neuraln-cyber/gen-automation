/* Editable scaffold for the reference format explicitly recommended by TenStrip.
 * https://huggingface.co/TenStrip/10Eros-Max/discussions/50
 * https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md
 * No image inference, LLM call, hidden prompt rewrite or invented visual details.
 */
(() => {
  "use strict";
  window.buildErosReferencePrompt = (motion, firstFrame = false) => {
    const sections = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"];
    if (sections.every((section) => motion.includes(`[${section}]`))) return motion;
    if (/\[(?:subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music|integrated_multimodal_description)\]/u.test(motion)) {
      throw new Error("This prompt already contains structured sections. Keep it intact and edit it using the linked reference guide; it was not rewritten.");
    }
    const picture = firstFrame ? "\n<Picture 1> is the first frame of [Shot 1]." : "";
    const retention = firstFrame ? "\n<Picture 1> ([Shot 1] first frame): fully_preserved - begin from the supplied image." : "";
    return `[subject_definitions]
<Subject 1> is the visible subject in <Picture 1>; use the image for appearance and identity.
<Subject 2> is the visual style of <Picture 1>, including its linework, colors and rendering style.
<Subject 3> is the scene and background shown in <Picture 1>.${picture}

[summary]
[${firstFrame ? "keyframe completion + reference generation" : "reference generation"}] Animate <Subject 1> in <Subject 3>, preserving <Subject 2>, with the action described below.

[retention_analysis]
<Subject 1> (appears in [Shot 1]): fully_preserved - retain appearance, identity and visible details from the reference.
<Subject 2> (appears in [Shot 1]): fully_preserved - retain the reference's visual style.
<Subject 3> (appears in [Shot 1]): fully_preserved - retain the reference scene.${retention}

[detailed_description]
The target video retains <Subject 2>, the visual style shown in <Picture 1>.
[Shot 1] ${firstFrame ? "The shot begins from <Picture 1>. " : ""}<Subject 1> appears in <Subject 3>.
${motion}

[overall_soundscape]
Describe the intended ambience, physical sounds and dialogue here, or specify silence.

[non_diegetic_music]
Describe the intended background music here, or specify none.`;
  };
})();
