(() => {
  "use strict";

  const studio = document.querySelector("[data-video-studio]");
  if (studio) {
    const sourceSelect = studio.querySelector("[data-video-source-select]");
    const sourcePreview = studio.querySelector("[data-video-source-preview]");
    const sourceCaption = studio.querySelector("[data-video-source-caption]");
    const rating = studio.querySelector("[data-video-rating]");
    const adultGroup = studio.querySelector("[data-video-adult-attestations]");
    const duration = studio.querySelector("[data-video-duration]");
    const variants = studio.querySelector("[data-video-variants]");
    const estimate = studio.querySelector("[data-video-cost-estimate]");

    const updateSource = () => {
      const option = sourceSelect?.selectedOptions?.[0];
      if (!option) return;
      if (sourcePreview && option.dataset.previewUrl) {
        sourcePreview.src = option.dataset.previewUrl;
      }
      if (sourceCaption) sourceCaption.textContent = option.dataset.sourceSize || "";
    };

    const updateAdultAttestations = () => {
      if (!adultGroup || !rating) return;
      const isAdult = rating.value === "nsfw" || rating.value === "explicit";
      adultGroup.hidden = !isAdult;
      adultGroup.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
        checkbox.required = isAdult;
        checkbox.disabled = !isAdult;
        if (!isAdult) checkbox.checked = false;
      });
    };

    const updateEstimate = () => {
      if (!duration || !variants || !estimate) return;
      const hourly = Number.parseFloat(studio.dataset.hourlyRateUsd || "0");
      const seconds = Number.parseInt(studio.dataset.planningRuntimeSeconds || "0", 10);
      const count = Number.parseInt(variants.value, 10) || 1;
      const dollars = hourly * (seconds / 3600) * count;
      estimate.textContent = `$${dollars.toFixed(3)}`;
    };

    sourceSelect?.addEventListener("change", updateSource);
    rating?.addEventListener("change", updateAdultAttestations);
    duration?.addEventListener("change", updateEstimate);
    variants?.addEventListener("change", updateEstimate);
    updateSource();
    updateAdultAttestations();
    updateEstimate();
  }

  const statusPage = document.querySelector("[data-video-status-refresh]");
  if (statusPage) {
    const seconds = Number.parseInt(statusPage.dataset.videoStatusRefresh || "0", 10);
    if (seconds > 0) {
      window.setTimeout(() => window.location.reload(), seconds * 1000);
    }
  }
})();
