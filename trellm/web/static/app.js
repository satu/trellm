(function() {
    "use strict";

    const REFRESH_INTERVAL = 5000;
    let countdown = 5;
    let timer = null;
    let activeStream = null;  // Current EventSource for live output
    let activeCardId = null;  // Card ID being streamed
    let historyData = [];     // Cached history data for sorting
    let historySortKey = "when";
    let historySortAsc = false;

    async function fetchJSON(url) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
    }

    function formatDuration(seconds) {
        if (seconds < 60) return seconds + "s";
        if (seconds < 3600) {
            const m = Math.floor(seconds / 60);
            const s = seconds % 60;
            return m + "m " + s + "s";
        }
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return h + "h " + m + "m";
    }

    function formatTime(isoString) {
        if (!isoString) return "-";
        const d = new Date(isoString);
        const now = new Date();
        const diffMs = now - d;
        const diffMin = Math.floor(diffMs / 60000);
        if (diffMin < 1) return "just now";
        if (diffMin < 60) return diffMin + "m ago";
        const diffH = Math.floor(diffMin / 60);
        if (diffH < 24) return diffH + "h ago";
        const diffD = Math.floor(diffH / 24);
        return diffD + "d ago";
    }

    function renderStatus(data) {
        document.getElementById("uptime").textContent = formatDuration(data.uptime_seconds);
        document.getElementById("poll-interval").textContent = data.poll_interval + "s";
        document.getElementById("active-tasks").textContent = data.active_tasks;
        document.getElementById("project-count").textContent = Object.keys(data.projects).length;

        const badge = document.getElementById("status-badge");
        badge.textContent = data.status;
        badge.className = "badge " + data.status;
    }

    function renderTasks(data) {
        const tbody = document.getElementById("tasks-body");
        const table = document.getElementById("tasks-table");
        const empty = document.getElementById("tasks-empty");

        // Auto-close output panel if the streamed task is no longer running
        if (activeCardId && !data.tasks.some(function(t) { return t.card_id === activeCardId; })) {
            closeOutput();
        }

        if (data.tasks.length === 0) {
            table.classList.add("hidden");
            empty.classList.remove("hidden");
            return;
        }

        table.classList.remove("hidden");
        empty.classList.add("hidden");

        tbody.innerHTML = data.tasks.map(function(t) {
            const link = t.card_url
                ? '<a href="' + t.card_url + '" target="_blank">' + escapeHtml(t.card_name) + '</a>'
                : escapeHtml(t.card_name);
            var lineCount = t.output_lines ? ' (' + t.output_lines + ')' : '';
            var streamBtn = '<button class="btn-stream" onclick="viewOutput(\'' + t.card_id + '\', \'' + escapeHtml(t.card_name).replace(/'/g, "\\'") + '\')">View' + lineCount + '</button>';
            return '<tr><td>' + escapeHtml(t.project) + '</td><td>' + link + '</td><td>' + formatDuration(t.duration_seconds) + '</td><td>' + streamBtn + '</td></tr>';
        }).join("");
    }

    function renderProjects(data) {
        const container = document.getElementById("projects-list");
        if (data.projects.length === 0) {
            container.innerHTML = '<div class="empty-state">No projects configured</div>';
            return;
        }

        container.innerHTML = data.projects.map(function(p) {
            const aliases = p.aliases.length > 0
                ? '<span class="project-aliases">(' + p.aliases.join(", ") + ')</span>'
                : '';
            return '<div class="project-item">' +
                '<div><span class="project-name">' + escapeHtml(p.name) + '</span>' + aliases + '</div>' +
                '<div class="project-details">' +
                '<div><span class="label">Cost: </span>' + p.stats.total_cost_dollars + '</div>' +
                '<div><span class="label">Tickets: </span>' + p.stats.total_tickets + '</div>' +
                '<div><span class="label">Changes: </span>+' + p.stats.total_lines_added + ' -' + p.stats.total_lines_removed + '</div>' +
                '<div><span class="label">Last: </span>' + formatTime(p.last_activity) + '</div>' +
                '</div></div>';
        }).join("");
    }

    function renderStatsBlock(containerId, stats) {
        var el = document.getElementById(containerId);
        el.innerHTML = '<div class="stats-grid">' +
            stat("Cost", stats.total_cost_dollars) +
            stat("Tickets", stats.total_tickets) +
            stat("Avg/Ticket", stats.average_cost_dollars) +
            (stats.api_duration ? stat("API Time", stats.api_duration) : "") +
            (stats.wall_duration ? stat("Wall Time", stats.wall_duration) : "") +
            stat("Tokens", stats.total_tokens) +
            stat("Input", stats.input_tokens) +
            stat("Output", stats.output_tokens) +
            (stats.total_lines_added !== undefined ? stat("Lines +/-", "+" + stats.total_lines_added + " -" + stats.total_lines_removed) : "") +
            '</div>';
    }

    function stat(label, value) {
        return '<div class="stat"><span class="label">' + label + '</span><span class="value">' + value + '</span></div>';
    }

    function usageBar(label, pct, resetsAt) {
        var color = pct < 60 ? "#238636" : pct < 85 ? "#d29922" : "#da3633";
        return '<div class="usage-item">' +
            '<div class="usage-header"><span class="label">' + label + '</span><span class="value">' + Math.round(pct) + '%</span></div>' +
            '<div class="usage-bar-bg"><div class="usage-bar-fill" style="width:' + Math.min(pct, 100) + '%;background:' + color + '"></div></div>' +
            (resetsAt ? '<div class="usage-reset">resets ' + resetsAt + '</div>' : '') +
            '</div>';
    }

    function renderUsageLimits(data) {
        var el = document.getElementById("usage-limits");
        var ul = data.usage_limits;
        if (!ul || ul.error) {
            var errMsg = ul && ul.error ? escapeHtml(ul.error) : "Unavailable";
            if (errMsg.indexOf("429") !== -1) {
                errMsg = "Rate limited — click Refresh to retry";
            }
            el.innerHTML = '<div class="empty-state">' + errMsg + '</div>';
            return;
        }
        var html = "";
        if (ul.five_hour) html += usageBar("5-Hour Session", ul.five_hour.utilization, ul.five_hour.resets_at);
        if (ul.seven_day) html += usageBar("7-Day Weekly", ul.seven_day.utilization, ul.seven_day.resets_at);
        if (ul.seven_day_opus) html += usageBar("7-Day Opus", ul.seven_day_opus.utilization, ul.seven_day_opus.resets_at);
        if (ul.seven_day_sonnet) html += usageBar("7-Day Sonnet", ul.seven_day_sonnet.utilization, ul.seven_day_sonnet.resets_at);
        el.innerHTML = html || '<div class="empty-state">No usage data</div>';
    }

    function renderStats(data) {
        renderStatsBlock("stats-global", data.global);
        renderStatsBlock("stats-last30", data.last_30_days);

        var el = document.getElementById("stats-by-project");
        var keys = Object.keys(data.by_project);
        if (keys.length === 0) {
            el.innerHTML = '<div class="empty-state">No project stats</div>';
            return;
        }
        el.innerHTML = keys.map(function(name) {
            var ps = data.by_project[name];
            return '<h3 style="margin: 12px 0 6px; color: #58a6ff; font-size: 0.9rem;">' + escapeHtml(name) + '</h3>' +
                '<div class="stats-grid">' +
                stat("Cost", ps.total_cost_dollars) +
                stat("Tickets", ps.total_tickets) +
                stat("Avg", ps.average_cost_dollars) +
                stat("Changes", "+" + ps.total_lines_added + " -" + ps.total_lines_removed) +
                stat("Tokens", ps.total_tokens) +
                '</div>';
        }).join("");
    }

    function formatTokens(n) {
        if (!n) return "0";
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
        if (n >= 1000) return (n / 1000).toFixed(1) + "K";
        return "" + n;
    }

    function renderHistory(data) {
        historyData = (data.recent_history || []).slice().reverse();
        renderHistoryTable();
    }

    function renderHistoryTable() {
        var tbody = document.getElementById("history-body");
        if (historyData.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No history</td></tr>';
            return;
        }

        var sorted = historyData.slice();
        sorted.sort(function(a, b) {
            var va, vb;
            switch (historySortKey) {
                case "project": va = a.project || ""; vb = b.project || ""; break;
                case "cost": va = a.cost_cents || 0; vb = b.cost_cents || 0; break;
                case "duration": va = a.wall_duration_seconds || 0; vb = b.wall_duration_seconds || 0; break;
                case "when": va = a.processed_at || ""; vb = b.processed_at || ""; break;
                default: return 0;
            }
            if (va < vb) return historySortAsc ? -1 : 1;
            if (va > vb) return historySortAsc ? 1 : -1;
            return 0;
        });

        // Update sort indicators
        document.querySelectorAll("#history-table .sortable").forEach(function(th) {
            th.classList.remove("asc", "desc");
            if (th.getAttribute("data-sort") === historySortKey) {
                th.classList.add(historySortAsc ? "asc" : "desc");
            }
        });

        tbody.innerHTML = sorted.map(function(h) {
            var cost = "$" + (h.cost_cents / 100).toFixed(2);
            var dur = formatDuration(h.wall_duration_seconds || 0);
            var changes = "+" + (h.lines_added || 0) + " -" + (h.lines_removed || 0);
            var tokens = formatTokens((h.input_tokens || 0) + (h.output_tokens || 0));
            var when = formatTime(h.processed_at);
            return '<tr><td>' + escapeHtml(h.project) + '</td><td>' + cost + '</td><td>' + dur + '</td><td>' + changes + '</td><td>' + tokens + '</td><td>' + when + '</td></tr>';
        }).join("");
    }

    function escapeHtml(str) {
        var d = document.createElement("div");
        d.textContent = str || "";
        return d.innerHTML;
    }

    async function refresh() {
        try {
            var [status, tasks, projects, stats] = await Promise.all([
                fetchJSON("/api/status"),
                fetchJSON("/api/tasks"),
                fetchJSON("/api/projects"),
                fetchJSON("/api/stats"),
            ]);
            renderStatus(status);
            renderTasks(tasks);
            renderProjects(projects);
            renderUsageLimits(stats);
            renderStats(stats);
            renderHistory(stats);
        } catch (e) {
            var badge = document.getElementById("status-badge");
            badge.textContent = "error";
            badge.className = "badge error";
        }
    }

    // Tab switching
    document.addEventListener("click", function(e) {
        if (!e.target.classList.contains("tab")) return;
        var tabs = e.target.parentNode.querySelectorAll(".tab");
        var section = e.target.closest(".card");
        var contents = section.querySelectorAll(".tab-content");
        tabs.forEach(function(t) { t.classList.remove("active"); });
        contents.forEach(function(c) { c.classList.remove("active"); });
        e.target.classList.add("active");
        var target = e.target.getAttribute("data-tab");
        var map = { "global": "stats-global", "last30": "stats-last30", "by-project": "stats-by-project" };
        document.getElementById(map[target]).classList.add("active");
    });

    // Control actions
    function showControlStatus(msg, isError) {
        var el = document.getElementById("control-status");
        el.textContent = msg;
        el.className = "control-status" + (isError ? " error" : " success");
        el.classList.remove("hidden");
        setTimeout(function() { el.classList.add("hidden"); }, 5000);
    }

    window.refreshUsage = function() {
        var btn = document.getElementById("btn-refresh-usage");
        btn.disabled = true;
        btn.textContent = "Refreshing...";
        fetch("/api/usage/refresh", { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success && data.usage_limits) {
                    renderUsageLimits({ usage_limits: data.usage_limits });
                }
            })
            .catch(function() {})
            .finally(function() {
                btn.disabled = false;
                btn.textContent = "Refresh";
            });
    };

    window.confirmAbort = function() {
        if (!confirm("Abort all running tasks? This will cancel all in-progress work.")) return;
        document.getElementById("btn-abort").disabled = true;
        fetch("/api/abort", { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showControlStatus("Aborted " + data.tasks_cancelled + " task(s)", false);
                    refresh();
                } else {
                    showControlStatus("Abort failed: " + (data.error || "unknown"), true);
                }
            })
            .catch(function(e) { showControlStatus("Abort failed: " + e.message, true); })
            .finally(function() { document.getElementById("btn-abort").disabled = false; });
    };

    window.viewOutput = function(cardId, cardName) {
        // Close any existing stream
        if (activeStream) {
            activeStream.close();
            activeStream = null;
        }
        activeCardId = cardId;

        var section = document.getElementById("output-section");
        var content = document.getElementById("output-content");
        var taskName = document.getElementById("output-task-name");

        taskName.textContent = cardName;
        content.textContent = "";
        section.classList.remove("hidden");

        // Connect to SSE stream
        activeStream = new EventSource("/api/stream/" + cardId);

        activeStream.onmessage = function(e) {
            content.textContent += e.data + "\n";
            if (document.getElementById("output-autoscroll").checked) {
                content.scrollTop = content.scrollHeight;
            }
        };

        activeStream.addEventListener("done", function() {
            content.textContent += "\n--- Task completed ---\n";
            activeStream.close();
            activeStream = null;
            activeCardId = null;
        });

        activeStream.onerror = function() {
            activeStream.close();
            activeStream = null;
            activeCardId = null;
        };
    };

    window.sortHistory = function(key) {
        if (historySortKey === key) {
            historySortAsc = !historySortAsc;
        } else {
            historySortKey = key;
            historySortAsc = true;
        }
        renderHistoryTable();
    };

    window.closeOutput = function() {
        if (activeStream) {
            activeStream.close();
            activeStream = null;
        }
        activeCardId = null;
        document.getElementById("output-section").classList.add("hidden");
        document.getElementById("output-content").textContent = "";
    };

    window.confirmRestart = function() {
        if (!confirm("Restart TreLLM? This will cancel all tasks and restart the process.")) return;
        document.getElementById("btn-restart").disabled = true;
        fetch("/api/restart", { method: "POST" })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showControlStatus("Restart initiated, page will reconnect...", false);
                } else {
                    showControlStatus("Restart failed: " + (data.error || "unknown"), true);
                }
            })
            .catch(function(e) { showControlStatus("Restart initiated, reconnecting...", false); })
            .finally(function() { document.getElementById("btn-restart").disabled = false; });
    };

    // Countdown timer
    function tick() {
        countdown--;
        document.getElementById("refresh-countdown").textContent = countdown;
        if (countdown <= 0) {
            countdown = REFRESH_INTERVAL / 1000;
            refresh();
        }
    }

    // Load config once (doesn't change often)
    async function loadConfig() {
        try {
            var data = await fetchJSON("/api/config");
            document.getElementById("config-content").textContent = JSON.stringify(data, null, 2);
        } catch (e) {
            document.getElementById("config-content").textContent = "Failed to load config";
        }
    }

    // Initial load
    refresh();
    loadConfig();
    timer = setInterval(tick, 1000);
})();
