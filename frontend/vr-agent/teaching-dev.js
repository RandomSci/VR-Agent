(() => {
  "use strict";

  const $ = (id) =>
    document.getElementById(id);

  const frame =
    $("n8n-teaching-frame");

  const panel =
    $("teaching-dev-controls");

  const teacher =
    $("td-teacher");

  const url =
    $("td-n8n-url");

  const status =
    $("td-status");

  const params =
    new URLSearchParams(
      location.search
    );

  url.value =
    params.get("n8n") ||
    localStorage.getItem(
      "teaching-dev-n8n-url"
    ) ||
    "";

  const state = {
    active: false,
    teacher: "mika",
    phase: "idle",
  };

  function renderStatus(message) {
    status.textContent =
      message ||
      `${
        state.active
          ? "TEACHING"
          : "ROOM"
      } • ${
        state.teacher.toUpperCase()
      } • ${
        state.phase
      }`;
  }

  function roomReady() {
    return !!(
      window.vrRoom &&
      typeof window.vrRoom.teaching
        === "function"
    );
  }

  function setFrameUrl(next) {
    const value =
      String(next || "").trim();

    if (!value) {
      return false;
    }

    try {
      const parsed =
        new URL(value);

      if (
        !/^https?:$/.test(
          parsed.protocol
        )
      ) {
        return false;
      }

      localStorage.setItem(
        "teaching-dev-n8n-url",
        parsed.href
      );

      frame.src =
        parsed.href;

      return true;

    } catch (_) {
      return false;
    }
  }

  function enter(
    nextTeacher = teacher.value
  ) {
    if (!roomReady()) {
      renderStatus(
        "Room renderer is still loading..."
      );

      return false;
    }

    const chosen =
      nextTeacher === "luna"
        ? "luna"
        : "mika";

    if (!setFrameUrl(url.value)) {
      renderStatus(
        "Paste the n8n workflow/project URL first."
      );

      return false;
    }

    if (
      !window.vrRoom.teaching(
        chosen
      )
    ) {
      renderStatus(
        `${chosen} is not loaded yet.`
      );

      return false;
    }

    state.active = true;
    state.teacher = chosen;
    state.phase = "teaching";

    teacher.value = chosen;

    document.documentElement
      .classList
      .add("vrr-teaching-mode");

    renderStatus();

    return true;
  }

  function switchTeacher(
    nextTeacher = teacher.value
  ) {
    if (!state.active) {
      return enter(nextTeacher);
    }

    const chosen =
      nextTeacher === "luna"
        ? "luna"
        : "mika";

    if (
      !window.vrRoom.teaching(
        chosen
      )
    ) {
      return false;
    }

    state.teacher = chosen;
    teacher.value = chosen;

    renderStatus();

    return true;
  }

  function exit() {
    if (
      window.vrRoom &&
      typeof window.vrRoom.normal
        === "function"
    ) {
      window.vrRoom.normal();
    }

    state.active = false;
    state.phase = "idle";

    document.documentElement
      .classList
      .remove("vrr-teaching-mode");

    renderStatus();
  }

  $("td-enter")
    .addEventListener(
      "click",
      () => enter()
    );

  $("td-switch")
    .addEventListener(
      "click",
      () => switchTeacher()
    );

  $("td-exit")
    .addEventListener(
      "click",
      exit
    );

  $("td-hide")
    .addEventListener(
      "click",
      () => {
        panel.classList.toggle(
          "td-collapsed"
        );
      }
    );

  teacher.addEventListener(
    "change",
    () => {
      if (state.active) {
        switchTeacher(
          teacher.value
        );
      }
    }
  );

  window.teachingDev =
    Object.freeze({
      enter,
      switchTeacher,
      exit,

      state: () => ({
        ...state,
        n8n: frame.src,
      }),
    });

  /*
   * Never show LIVE on this
   * development page.
   */
  const badgeTimer =
    setInterval(() => {
      const live =
        document.querySelector(
          ".vra-live"
        );

      const title =
        document.querySelector(
          ".vra-title"
        );

      if (!live) return;

      live.textContent = "DEV";

      if (title) {
        title.textContent =
          "TEACHING DEV";
      }

      clearInterval(
        badgeTimer
      );
    }, 250);

  renderStatus(
    "DEV ROOM • waiting for Mika & Luna"
  );
})();
