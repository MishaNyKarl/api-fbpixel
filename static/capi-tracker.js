(function () {
  if (window.__CAPI_TRACKER_LOADED__) {
    console.warn('[CAPI] tracker already loaded, skipping duplicate init');
    return;
  }
  window.__CAPI_TRACKER_LOADED__ = true;

  const CONFIG = {
    apiUrl: "https://api.naturalgoods.info/api/pixel/track",
    apiKey: "CHANGE_ME_MIN_16CHARS",
    trackerPixelId: "px_REPLACE_ME",

    formId: "order_form",
    nameInputId: "name-input",
    phoneInputId: "phone-input",

    currency: "USD",
    value: 0
  };

  function getCookie(n) {
    return (document.cookie.match('(?:^|; )' + n + '=([^;]*)') || [])[1];
  }

  function setCookie(n, v, d) {
    const e = new Date();
    e.setTime(e.getTime() + d * 864e5);
    document.cookie = n + '=' + v + '; path=/; expires=' + e.toUTCString();
  }

  function ensureFbp() {
    let v = getCookie('_fbp');
    if (!v) {
      v = 'fb.1.' + Date.now() + '.' + Math.floor(Math.random() * 1e10);
      setCookie('_fbp', v, 180);
    }
    return v;
  }

  function ensureFbc() {
    let v = getCookie('_fbc');
    if (v) {
      return v;
    }
    const p = new URLSearchParams(location.search);
    const fbclid = p.get('fbclid');
    if (!fbclid) {
      return '';
    }
    v = 'fb.1.' + Date.now() + '.' + fbclid;
    setCookie('_fbc', v, 90);
    return v;
  }

  function qs(n) {
    return new URLSearchParams(location.search).get(n) || '';
  }

  function getClickId() {
    return qs('clickid') || qs('click_id') || qs('subid') || qs('sub_id');
  }

  async function sendEvent(eventName, payload, options = {}) {
    const body = Object.assign({
      tracker_pixel_id: CONFIG.trackerPixelId,
      event_name: eventName,
      clickid: getClickId(),
      fbclid: qs('fbclid'),
      fbp: ensureFbp(),
      fbc: ensureFbc(),
      event_source_url: location.href,
      ua: navigator.userAgent
    }, payload || {});

    console.log('[CAPI] sending', eventName, body);

    try {
      const resp = await fetch(CONFIG.apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Api-Key": CONFIG.apiKey
        },
        body: JSON.stringify(body),
        keepalive: options.keepalive || false
      });

      const text = await resp.text();
      console.log('[CAPI] response', resp.status, text);

      return { ok: resp.ok, status: resp.status, text };
    } catch (e) {
      console.error("[CAPI] fetch error", e);
      return { ok: false, error: String(e) };
    }
  }

  window.addEventListener('error', function (e) {
    console.error('[CAPI] window error', e.message);
  });

  window.addEventListener('unhandledrejection', function (e) {
    console.error('[CAPI] unhandled', e.reason);
  });

  document.addEventListener("DOMContentLoaded", async function () {
    await sendEvent("PageView", {});

    const form = document.getElementById(CONFIG.formId);

    if (!form) {
      console.warn('[CAPI] form not found:', CONFIG.formId);
      return;
    }

    let leadSubmitting = false;

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (leadSubmitting) {
        console.warn('[CAPI] duplicate submit ignored');
        return;
      }
      leadSubmitting = true;

      try {
        const nameInput = document.getElementById(CONFIG.nameInputId);
        const phoneInput = document.getElementById(CONFIG.phoneInputId);

        const name = nameInput?.value?.trim() || "";
        const phone = phoneInput?.value?.trim() || "";

        await sendEvent("Lead", {
          user_data_raw: {
            email: "",
            phone: phone
          },
          extra: {
            name: name,
            country: form.country_code?.value || ""
          },
          currency: CONFIG.currency,
          value: CONFIG.value
        }, {
          keepalive: true
        });
      } finally {
        setTimeout(function () {
          form.submit();
        }, 150);
      }
    });
  });
})();
