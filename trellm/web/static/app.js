(function() {
    "use strict";

    const REFRESH_INTERVAL = 5000;
    let countdown = 5;
    let timer = null;

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
            return '<tr><td>' + escapeHtml(t.project) + '</td><td>' + link + '</td><td>' + formatDuration(t.duration_seconds) + '</td></tr>';
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
            el.innerHTML = '<div class="empty-state">' + (ul && ul.error ? escapeHtml(ul.error) : "Unavailable") + '</div>';
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

    function renderHistory(data) {
        var tbody = document.getElementById("history-body");
        var entries = (data.recent_history || []).slice().reverse();
        if (entries.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No history</td></tr>';
            return;
        }
        tbody.innerHTML = entries.map(function(h) {
            var cost = "$" + (h.cost_cents / 100).toFixed(2);
            var dur = formatDuration(h.wall_duration_seconds || 0);
            var changes = "+" + (h.lines_added || 0) + " -" + (h.lines_removed || 0);
            var when = formatTime(h.processed_at);
            return '<tr><td>' + escapeHtml(h.project) + '</td><td>' + cost + '</td><td>' + dur + '</td><td>' + changes + '</td><td>' + when + '</td></tr>';
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

    // Countdown timer
    function tick() {
        countdown--;
        document.getElementById("refresh-countdown").textContent = countdown;
        if (countdown <= 0) {
            countdown = REFRESH_INTERVAL / 1000;
            refresh();
        }
    }

    // Initial load
    refresh();
    timer = setInterval(tick, 1000);
})();
