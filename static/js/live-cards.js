// Keeps an open admin page in sync with the hub: polls /cards/state and
// refreshes the page once the hub's card set changed, so a card tapped on an
// ESPuino shows up by itself instead of waiting for the admin to press F5.
// Polling rather than a server push is deliberate — see cards_state() in
// app.py for why an SSE stream would be the wrong shape here.
(function () {
	"use strict";

	var POLL_MS = 5000;

	document.addEventListener("DOMContentLoaded", function () {
		var marker = document.getElementById("live-cards");
		if (!marker || !window.fetch) {
			return;
		}

		var stateUrl = marker.dataset.stateUrl;
		var revision = marker.dataset.revision;
		var banner = document.getElementById("live-cards-banner");
		var reloadButton = document.getElementById("live-cards-reload");
		var stopped = false;
		var timer = null;

		if (reloadButton) {
			reloadButton.addEventListener("click", function () {
				window.location.reload();
			});
		}

		// Reloading while a card ID is being typed into the add-card form, or
		// while the duplicate dialog is open, would throw that away. Those
		// cases get the banner and reload when the admin says so.
		function isBusy() {
			var active = document.activeElement;
			if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) {
				return true;
			}
			return document.querySelector("dialog[open]") !== null;
		}

		function schedule() {
			if (!stopped) {
				timer = window.setTimeout(poll, POLL_MS);
			}
		}

		function poll() {
			if (document.hidden) {
				// Nothing worth refreshing in a background tab; the
				// visibilitychange handler below polls as soon as it returns.
				schedule();
				return;
			}

			fetch(stateUrl, {headers: {"Accept": "application/json"}, credentials: "same-origin"})
				.then(function (response) {
					var contentType = response.headers.get("content-type") || "";
					if (!response.ok || contentType.indexOf("application/json") === -1) {
						// Usually a hub-password session that expired: the
						// request followed the redirect to the login page. Stop
						// instead of polling a login form for the rest of the day.
						stopped = true;
						return null;
					}
					return response.json();
				})
				.then(function (state) {
					if (!state || state.revision === revision) {
						schedule();
						return;
					}
					if (isBusy()) {
						if (banner) {
							banner.hidden = false;
						}
						schedule();
						return;
					}
					window.location.reload();
				})
				.catch(function () {
					// Hub restarting or a network hiccup — keep trying quietly.
					schedule();
				});
		}

		document.addEventListener("visibilitychange", function () {
			if (!document.hidden && !stopped) {
				window.clearTimeout(timer);
				poll();
			}
		});

		schedule();
	});
})();
