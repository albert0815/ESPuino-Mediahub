// ARD Sounds picker for the card assignment form: search a show, then pick
// either "always the newest" or a fixed set of episodes.
//
// Same house rules as media-browser.js — vanilla JS, no framework, no CDN,
// all catalogue calls go through the hub's own /podcast/* endpoints (so the
// browser never talks to ARD directly). Everything the form submits ends up
// in one hidden JSON field; the server re-validates it (_parse_podcast_form).
(function () {
	"use strict";

	function initPodcastPicker(section) {
		var config = JSON.parse(document.getElementById("podcast-config").textContent);
		var labels = config.labels;
		var singleFileModes = (config.singleFileModes || []).map(String);

		var sourceArd = document.getElementById("podcast-source-ard");
		var sourceRss = document.getElementById("podcast-source-rss");
		var ardRow = document.getElementById("podcast-ard-row");
		var rssRow = document.getElementById("podcast-rss-row");
		var feedInput = document.getElementById("podcast-feed-url");
		var feedBtn = document.getElementById("podcast-feed-btn");
		var feedStatus = document.getElementById("podcast-feed-status");
		var queryInput = document.getElementById("podcast-query");
		var searchBtn = document.getElementById("podcast-search-btn");
		var resultsEl = document.getElementById("podcast-results");
		var showRow = document.getElementById("podcast-show-row");
		var showEl = document.getElementById("podcast-show");
		var selectionRow = document.getElementById("podcast-selection-row");
		var latestRadio = document.getElementById("podcast-selection-latest");
		var episodesRadio = document.getElementById("podcast-selection-episodes");
		var countRow = document.getElementById("podcast-count-row");
		var countInput = document.getElementById("podcast-count");
		var episodesEl = document.getElementById("podcast-episodes");
		var playModeRow = document.getElementById("podcast-play-mode-row");
		var playModeSelect = document.getElementById("podcast_play_mode");
		var hiddenInput = document.getElementById("podcast-json");

		// The whole picker state, mirrored into the hidden field on every change.
		var state = {
			source: config.sources.ard,
			show_id: null,
			feed_url: null,
			show_urn: null,
			show_title: "",
			show_image: null,
			selection: "latest",
			episode_count: 1,
			episodes: []
		};
		// Episodes loaded from the catalogue so far (newest first), and how many
		// of them are already on screen — "Load more" pages through the show.
		var loadedEpisodes = [];
		var episodesTotal = 0;
		var episodesLoading = false;

		function isSingleFileMode() {
			return singleFileModes.indexOf(playModeSelect.value) !== -1;
		}

		function save() {
			hiddenInput.value = JSON.stringify(state);
		}

		function text(tag, className, content) {
			var el = document.createElement(tag);
			if (className) {
				el.className = className;
			}
			if (content !== undefined && content !== null) {
				el.textContent = content;
			}
			return el;
		}

		function formatDate(iso) {
			if (!iso) {
				return "";
			}
			var date = new Date(iso);
			return isNaN(date.getTime()) ? String(iso).slice(0, 10) : date.toLocaleDateString();
		}

		function formatDuration(seconds) {
			if (!seconds) {
				return "";
			}
			var minutes = Math.round(seconds / 60);
			return minutes + " min";
		}

		// "Die Maus · 950 episodes · latest episode: 21/09/2026" — the date is
		// what tells you at a glance whether a show is still running or was
		// last touched years ago, which the episode count alone does not.
		function showMeta(show) {
			var parts = [];
			if (show.publisher) {
				parts.push(show.publisher);
			}
			if (show.episode_count) {
				parts.push(labels.episodeCount.replace("{num}", show.episode_count));
			}
			if (show.last_item_added) {
				parts.push(labels.latestEpisode.replace("{date}", formatDate(show.last_item_added)));
			}
			return parts.join(" · ");
		}

		function setMessage(container, message, isError) {
			container.hidden = false;
			container.innerHTML = "";
			container.appendChild(text("p", isError ? "podcast-msg podcast-error" : "podcast-msg muted", message));
		}

		// -- search ------------------------------------------------------
		function search() {
			var query = queryInput.value.trim();
			if (!query) {
				return;
			}
			setMessage(resultsEl, labels.searching, false);
			fetch(config.searchUrl + "?q=" + encodeURIComponent(query))
				.then(function (response) {
					return response.json().then(function (data) {
						if (!response.ok) {
							throw new Error(data.error || labels.searchFailed);
						}
						return data;
					});
				})
				.then(function (data) {
					renderResults(data.shows || []);
				})
				.catch(function (error) {
					setMessage(resultsEl, error.message || labels.searchFailed, true);
				});
		}

		function renderResults(shows) {
			if (!shows.length) {
				setMessage(resultsEl, labels.noResults, false);
				return;
			}
			resultsEl.hidden = false;
			resultsEl.innerHTML = "";
			shows.forEach(function (show) {
				var row = text("div", "podcast-result");
				if (show.image_url) {
					var image = document.createElement("img");
					image.className = "podcast-cover";
					image.src = show.image_url;
					image.alt = "";
					image.loading = "lazy";
					image.width = 48;
					image.height = 48;
					row.appendChild(image);
				}

				var main = text("div", "podcast-result-main");
				main.appendChild(text("strong", null, show.title));
				main.appendChild(text("span", "podcast-meta", showMeta(show)));
				if (show.synopsis) {
					main.appendChild(text("span", "podcast-synopsis", show.synopsis));
				}
				row.appendChild(main);

				var button = text("button", "btn btn-small btn-secondary", labels.select);
				button.type = "button";
				button.addEventListener("click", function () {
					selectShow(show);
				});
				row.appendChild(button);
				resultsEl.appendChild(row);
			});
		}

		// -- podcast feed --------------------------------------------------
		function loadFeed() {
			var url = feedInput.value.trim();
			if (!url) {
				return;
			}
			setMessage(feedStatus, labels.loadingFeed, false);
			fetch(config.feedUrl + "?url=" + encodeURIComponent(url))
				.then(function (response) {
					return response.json().then(function (data) {
						if (!response.ok) {
							throw new Error(data.error || labels.feedFailed);
						}
						return data;
					});
				})
				.then(function (data) {
					feedStatus.hidden = true;
					state.feed_url = data.feed_url;
					state.show_id = null;
					state.show_urn = null;
					state.show_title = data.title || data.feed_url;
					state.show_image = data.image_url || null;
					state.episodes = [];
					// The feed came back whole, so there is nothing to page
					// through later — unlike the ARD catalogue.
					loadedEpisodes = data.episodes || [];
					episodesTotal = loadedEpisodes.length;
					renderShow({
						title: state.show_title,
						image_url: state.show_image,
						episode_count: episodesTotal,
						last_item_added: loadedEpisodes.length ? loadedEpisodes[0].publish_date : null
					});
					save();
					updateVisibility();
					if (state.selection === "episodes") {
						renderEpisodes();
					}
				})
				.catch(function (error) {
					setMessage(feedStatus, error.message || labels.feedFailed, true);
				});
		}

		function isRss() {
			return state.source === config.sources.rss;
		}

		// -- show selection ----------------------------------------------
		function selectShow(show) {
			state.show_id = String(show.id);
			state.show_urn = show.urn || null;
			state.show_title = show.title || "";
			state.show_image = show.image_url || null;
			state.episodes = [];
			loadedEpisodes = [];
			episodesTotal = 0;
			resultsEl.hidden = true;
			renderShow(show);
			save();
			updateVisibility();
			if (state.selection === "episodes") {
				loadEpisodes(true);
			}
		}

		function renderShow(show) {
			showRow.hidden = false;
			showEl.innerHTML = "";
			if (show.image_url) {
				var image = document.createElement("img");
				image.className = "podcast-cover";
				image.src = show.image_url;
				image.alt = "";
				image.width = 48;
				image.height = 48;
				showEl.appendChild(image);
			}
			var main = text("div", "podcast-result-main");
			main.appendChild(text("strong", null, show.title || state.show_title));
			var meta = showMeta(show);
			if (meta) {
				main.appendChild(text("span", "podcast-meta", meta));
			}
			showEl.appendChild(main);

			var change = text("button", "btn btn-small btn-secondary", labels.change);
			change.type = "button";
			change.addEventListener("click", function () {
				resultsEl.hidden = true;
				var input = isRss() ? feedInput : queryInput;
				input.focus();
				input.select();
			});
			showEl.appendChild(change);
		}

		// -- episodes ----------------------------------------------------
		function loadEpisodes(reset) {
			if (isRss()) {
				// The whole feed is already in memory.
				renderEpisodes();
				return;
			}
			if (!state.show_id || episodesLoading) {
				return;
			}
			if (reset) {
				loadedEpisodes = [];
				episodesTotal = 0;
			}
			if (!reset && loadedEpisodes.length >= episodesTotal && episodesTotal) {
				return;
			}
			episodesLoading = true;
			if (!loadedEpisodes.length) {
				setMessage(episodesEl, labels.loadingEpisodes, false);
			}
			var url = config.episodesUrlTemplate.replace("SHOW_ID", encodeURIComponent(state.show_id));
			fetch(url + "?limit=25&offset=" + loadedEpisodes.length)
				.then(function (response) {
					return response.json().then(function (data) {
						if (!response.ok) {
							throw new Error(data.error || labels.searchFailed);
						}
						return data;
					});
				})
				.then(function (data) {
					episodesTotal = data.total || 0;
					loadedEpisodes = loadedEpisodes.concat(data.episodes || []);
					renderEpisodes();
				})
				.catch(function (error) {
					setMessage(episodesEl, error.message || labels.searchFailed, true);
				})
				.finally(function () {
					episodesLoading = false;
				});
		}

		function isChosen(episodeId) {
			return state.episodes.some(function (episode) {
				return episode.id === String(episodeId);
			});
		}

		function toggleEpisode(episode, checked) {
			var entry = {
				id: String(episode.id),
				title: episode.title || "",
				publish_date: episode.publish_date || null,
				duration: episode.duration || 0
			};
			if (!checked) {
				state.episodes = state.episodes.filter(function (chosen) {
					return chosen.id !== entry.id;
				});
			} else if (isSingleFileMode()) {
				// Behaves like a radio group in the single-track modes, mirroring
				// what the library browser does for files.
				state.episodes = [entry];
			} else if (state.episodes.length >= config.maxEpisodes) {
				return false;
			} else if (!isChosen(entry.id)) {
				state.episodes.push(entry);
			}
			save();
			renderEpisodes();
			return true;
		}

		function renderEpisodes() {
			if (!loadedEpisodes.length) {
				setMessage(episodesEl, labels.noEpisodes, false);
				return;
			}
			episodesEl.hidden = false;
			episodesEl.innerHTML = "";
			loadedEpisodes.forEach(function (episode) {
				var row = text("label", "podcast-episode");
				var checkbox = document.createElement("input");
				checkbox.type = "checkbox";
				checkbox.checked = isChosen(episode.id);
				checkbox.addEventListener("change", function () {
					if (!toggleEpisode(episode, checkbox.checked)) {
						checkbox.checked = false;
					}
				});
				row.appendChild(checkbox);

				var main = text("span", "podcast-episode-main");
				main.appendChild(text("span", "podcast-episode-title", episode.title));
				var meta = [formatDate(episode.publish_date), formatDuration(episode.duration)]
					.filter(Boolean)
					.join(" · ");
				main.appendChild(text("span", "podcast-meta", meta));
				row.appendChild(main);
				episodesEl.appendChild(row);
			});

			if (loadedEpisodes.length < episodesTotal) {
				var more = text("button", "btn btn-small btn-secondary podcast-load-more", labels.loadMore);
				more.type = "button";
				more.addEventListener("click", function () {
					loadEpisodes(false);
				});
				episodesEl.appendChild(more);
			}
		}

		// -- wiring ------------------------------------------------------
		function updateVisibility() {
			ardRow.hidden = isRss();
			rssRow.hidden = !isRss();
			var hasShow = isRss() ? !!state.feed_url : !!state.show_id;
			showRow.hidden = !hasShow;
			selectionRow.hidden = !hasShow;
			playModeRow.hidden = !hasShow;
			countRow.hidden = state.selection !== "latest";
			episodesEl.hidden = state.selection !== "episodes" || !hasShow;
			countInput.disabled = isSingleFileMode();
			if (isSingleFileMode()) {
				countInput.value = 1;
				state.episode_count = 1;
				if (state.episodes.length > 1) {
					state.episodes = state.episodes.slice(0, 1);
				}
				countRow.title = labels.singleEpisodeOnly;
			} else {
				countRow.title = "";
			}
			save();
		}

		[sourceArd, sourceRss].forEach(function (radio) {
			radio.addEventListener("change", function () {
				state.source = radio.value;
				// Switching source invalidates whatever the other one picked.
				state.show_id = null;
				state.show_urn = null;
				state.feed_url = null;
				state.show_title = "";
				state.show_image = null;
				state.episodes = [];
				loadedEpisodes = [];
				episodesTotal = 0;
				resultsEl.hidden = true;
				feedStatus.hidden = true;
				showEl.innerHTML = "";
				save();
				updateVisibility();
			});
		});

		feedBtn.addEventListener("click", loadFeed);
		feedInput.addEventListener("keydown", function (event) {
			if (event.key === "Enter") {
				event.preventDefault();
				loadFeed();
			}
		});

		searchBtn.addEventListener("click", search);
		queryInput.addEventListener("keydown", function (event) {
			if (event.key === "Enter") {
				// Enter in the search box must search, not submit the half-filled
				// assignment form.
				event.preventDefault();
				search();
			}
		});

		[latestRadio, episodesRadio].forEach(function (radio) {
			radio.addEventListener("change", function () {
				state.selection = radio.value;
				save();
				updateVisibility();
				// A feed arrives complete, so there is nothing to fetch — but the
				// list still has to be drawn, which loadEpisodes does for both.
				if (state.selection === "episodes" && (isRss() || !loadedEpisodes.length)) {
					loadEpisodes(true);
				}
			});
		});

		countInput.addEventListener("input", function () {
			var value = parseInt(countInput.value, 10);
			if (isNaN(value) || value < 1) {
				value = 1;
			}
			state.episode_count = Math.min(value, config.maxEpisodes);
			save();
		});

		playModeSelect.addEventListener("change", function () {
			updateVisibility();
			if (state.selection === "episodes") {
				renderEpisodes();
			}
		});

		// Re-open an existing podcast card with its saved intent in place.
		if (config.initial && (config.initial.show_id || config.initial.feed_url)) {
			var initial = config.initial;
			state.source = initial.source === config.sources.rss ? config.sources.rss : config.sources.ard;
			state.feed_url = initial.feed_url || null;
			state.show_id = initial.show_id ? String(initial.show_id) : null;
			state.show_urn = initial.show_urn || null;
			state.show_title = initial.show_title || "";
			state.show_image = initial.show_image || null;
			state.selection = initial.selection === "episodes" ? "episodes" : "latest";
			state.episode_count = initial.episode_count || 1;
			state.episodes = (initial.episodes || []).map(function (episode) {
				return {
					id: String(episode.id),
					title: episode.title || "",
					publish_date: episode.publish_date || null,
					duration: episode.duration || 0
				};
			});
			countInput.value = state.episode_count;
			if (state.feed_url) {
				feedInput.value = state.feed_url;
			}
			renderShow({
				id: state.show_id,
				title: state.show_title,
				image_url: state.show_image
			});
			if (state.selection === "episodes") {
				loadEpisodes(true);
			}
		}

		(state.source === config.sources.rss ? sourceRss : sourceArd).checked = true;

		(state.selection === "episodes" ? episodesRadio : latestRadio).checked = true;
		updateVisibility();
	}

	document.addEventListener("DOMContentLoaded", function () {
		var section = document.getElementById("mh-podcast-section");
		if (section) {
			initPodcastPicker(section);
		}
	});
})();
