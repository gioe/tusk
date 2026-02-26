"""JavaScript bundle for tusk-dashboard.py.

Extracted from generate_js() to reduce the main file size.
"""

JS: str = """\
(function() {
  var body = document.getElementById('metricsBody');
  if (!body) return;
  var allRows = Array.prototype.slice.call(body.querySelectorAll('tr[data-task-id]'));
  var criteriaRows = {};
  body.querySelectorAll('tr.criteria-row').forEach(function(cr) {
    criteriaRows[cr.getAttribute('data-parent')] = cr;
  });
  var filtered = allRows.slice();
  var currentPage = 1;
  var pageSize = 10;
  var sortCol = 2;
  var sortAsc = false;
  var statusFilter = 'All';
  var searchTerm = '';
  var complexityFilter = '';

  var headers = document.querySelectorAll('#metricsTable > thead th');
  var statusSelect = document.getElementById('statusFilter');
  var searchInput = document.getElementById('searchInput');
  var complexitySelect = document.getElementById('complexityFilter');
  var filterBadge = document.getElementById('filterBadge');
  var clearBtn = document.getElementById('clearFilters');
  var pageSizeEl = document.getElementById('pageSize');
  var prevBtn = document.getElementById('prevPage');
  var nextBtn = document.getElementById('nextPage');
  var pageInfo = document.getElementById('pageInfo');
  // Populate dropdown options from row data
  function populateSelect(select, attr, placeholder) {
    var values = {};
    allRows.forEach(function(row) {
      var v = row.getAttribute(attr) || '';
      if (v) values[v] = true;
    });
    var sorted = Object.keys(values).sort();
    select.innerHTML = '<option value="">' + placeholder + '</option>';
    sorted.forEach(function(v) {
      var opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
  }

  var complexityOrder = ['XS', 'S', 'M', 'L', 'XL'];
  function populateComplexitySelect() {
    var values = {};
    allRows.forEach(function(row) {
      var v = row.getAttribute('data-complexity') || '';
      if (v) values[v] = true;
    });
    complexitySelect.innerHTML = '<option value="">Size</option>';
    complexityOrder.forEach(function(v) {
      if (values[v]) {
        var opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        complexitySelect.appendChild(opt);
      }
    });
  }

  populateComplexitySelect();

  // --- URL hash state ---
  var hashUpdateTimer = null;

  function encodeHashState() {
    var params = [];
    if (statusFilter !== 'All') params.push('s=' + encodeURIComponent(statusFilter));
    if (complexityFilter) params.push('c=' + encodeURIComponent(complexityFilter));
    if (searchTerm) params.push('q=' + encodeURIComponent(searchTerm));
    if (sortCol !== 13) params.push('sc=' + sortCol);
    if (sortAsc) params.push('sa=1');
    if (currentPage !== 1) params.push('p=' + currentPage);
    if (pageSize !== 10) params.push('ps=' + pageSize);
    return params.length > 0 ? params.join('&') : '';
  }

  function pushHashState() {
    if (hashUpdateTimer) clearTimeout(hashUpdateTimer);
    hashUpdateTimer = setTimeout(function() {
      var hash = encodeHashState();
      var newUrl = window.location.pathname + (hash ? '#' + hash : '');
      history.replaceState(null, '', newUrl);
    }, 100);
  }

  function restoreHashState() {
    var hash = window.location.hash.replace(/^#/, '');
    if (!hash) return false;
    var pairs = hash.split('&');
    var restored = false;
    pairs.forEach(function(pair) {
      var kv = pair.split('=');
      var k = kv[0];
      var v = decodeURIComponent(kv.slice(1).join('='));
      switch (k) {
        case 's': statusFilter = v; restored = true; break;
        case 'c': complexityFilter = v; restored = true; break;
        case 'q': searchTerm = v; restored = true; break;
        case 'sc': sortCol = parseInt(v) || 2; restored = true; break;
        case 'sa': sortAsc = v === '1'; restored = true; break;
        case 'p': currentPage = parseInt(v) || 1; restored = true; break;
        case 'ps': pageSize = parseInt(v) || 10; restored = true; break;
      }
    });
    return restored;
  }

  function syncUIFromState() {
    // Status dropdown
    statusSelect.value = statusFilter;
    complexitySelect.value = complexityFilter;
    // Search
    searchInput.value = searchTerm;
    // Page size
    pageSizeEl.value = pageSize.toString();
    // Sort header highlight
    headers.forEach(function(h) {
      h.classList.remove('sort-asc', 'sort-desc');
      h.querySelector('.sort-arrow').textContent = '\\u25B2';
    });
    if (sortCol >= 0 && sortCol < headers.length) {
      headers[sortCol].classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      headers[sortCol].querySelector('.sort-arrow').textContent = sortAsc ? '\\u25B2' : '\\u25BC';
    }
  }

  // --- Active filter badge ---
  function updateFilterBadge() {
    var count = 0;
    if (statusFilter !== 'All') count++;
    if (complexityFilter) count++;
    if (searchTerm) count++;
    if (count > 0) {
      filterBadge.textContent = count;
      filterBadge.classList.remove('hidden');
      clearBtn.classList.remove('hidden');
    } else {
      filterBadge.classList.add('hidden');
      clearBtn.classList.add('hidden');
    }
  }

  function clearAllFilters() {
    statusFilter = 'All';
    complexityFilter = '';
    searchTerm = '';
    syncUIFromState();
    applyFilter();
  }

  function formatCost(n) {
    return '$' + n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
  }

  function formatTokensCompact(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return Math.round(n).toString();
  }

  function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '0m';
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function formatLinesHtml(totalLines) {
    // We only have the total for filtering; full HTML comes from server
    return totalLines > 0 ? totalLines.toString() : '\\u2014';
  }

  function applyFilter() {
    filtered = allRows.filter(function(row) {
      if (statusFilter !== 'All' && row.getAttribute('data-status') !== statusFilter) return false;
      if (complexityFilter && row.getAttribute('data-complexity') !== complexityFilter) return false;
      if (searchTerm && row.getAttribute('data-summary').indexOf(searchTerm) === -1) return false;
      return true;
    });
    currentPage = 1;
    updateFilterBadge();
    pushHashState();
    render();
  }

  function applySort() {
    if (sortCol < 0) return;
    var type = headers[sortCol].getAttribute('data-type');
    filtered.sort(function(a, b) {
      var cellA = a.children[sortCol];
      var cellB = b.children[sortCol];
      var vA, vB;
      if (type === 'num') {
        vA = parseFloat(cellA.getAttribute('data-sort')) || 0;
        vB = parseFloat(cellB.getAttribute('data-sort')) || 0;
      } else {
        vA = (cellA.getAttribute('data-sort') || cellA.textContent || '').toLowerCase();
        vB = (cellB.getAttribute('data-sort') || cellB.textContent || '').toLowerCase();
      }
      if (vA < vB) return sortAsc ? -1 : 1;
      if (vA > vB) return sortAsc ? 1 : -1;
      return 0;
    });
    pushHashState();
    render();
  }

  function isFiltered() {
    return statusFilter !== 'All' || complexityFilter || searchTerm;
  }

  function render() {
    allRows.forEach(function(r) { r.style.display = 'none'; });
    Object.keys(criteriaRows).forEach(function(k) { criteriaRows[k].style.display = 'none'; });

    var start, end;
    if (pageSize === 0) {
      start = 0;
      end = filtered.length;
    } else {
      var maxPage = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (currentPage > maxPage) currentPage = maxPage;
      start = (currentPage - 1) * pageSize;
      end = Math.min(start + pageSize, filtered.length);
    }

    for (var i = 0; i < filtered.length; i++) {
      body.appendChild(filtered[i]);
      var tid = filtered[i].getAttribute('data-task-id');
      if (tid && criteriaRows[tid]) {
        body.appendChild(criteriaRows[tid]);
      }
    }
    for (var j = start; j < end; j++) {
      filtered[j].style.display = '';
      var jtid = filtered[j].getAttribute('data-task-id');
      if (jtid && criteriaRows[jtid] && filtered[j].classList.contains('expanded')) {
        criteriaRows[jtid].style.display = '';
      }
    }

    if (pageSize === 0) {
      pageInfo.textContent = filtered.length + ' tasks';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
    } else {
      var maxP = Math.max(1, Math.ceil(filtered.length / pageSize));
      pageInfo.textContent = 'Page ' + currentPage + ' of ' + maxP + ' (' + filtered.length + ' tasks)';
      prevBtn.disabled = currentPage <= 1;
      nextBtn.disabled = currentPage >= maxP;
    }
  }

  // --- Criteria client-side rendering engine ---
  var CDATA = window.CRITERIA_DATA || {};
  var criteriaRendered = {};

  function escHtml(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  function fmtDate(s) {
    if (!s) return '';
    return s.replace(/\.\d+$/, '');
  }

  function fmtRelTime(ms) {
    if (ms < 1000) return '+0s';
    var s = Math.round(ms / 1000);
    if (s < 60) return '+' + s + 's';
    var m = Math.floor(s / 60);
    var rs = s % 60;
    return '+' + m + 'm ' + rs + 's';
  }

  var TOOL_COLOR_MAP = {
    'Bash': '#f59e0b', 'Read': '#3b82f6', 'Edit': '#22c55e', 'Write': '#a855f7',
    'Grep': '#06b6d4', 'Glob': '#0ea5e9', 'WebFetch': '#ec4899', 'WebSearch': '#db2777',
    'Task': '#ef4444', 'TaskOutput': '#dc2626', 'TaskStop': '#b91c1c',
    'NotebookEdit': '#8b5cf6', 'AskUserQuestion': '#64748b', 'ExitPlanMode': '#64748b',
    'EnterPlanMode': '#64748b'
  };
  function toolColor(name) {
    if (TOOL_COLOR_MAP[name]) return TOOL_COLOR_MAP[name];
    if (name && name.indexOf('mcp__') === 0) return '#f97316';
    return '#6b7280';
  }

  function renderCriterionTimeline(events) {
    if (!events || events.length === 0) return '';
    var total = 0;
    events.forEach(function(e) { total += e.cost_dollars || 0; });
    var t0 = null;
    try { t0 = new Date(events[0].called_at).getTime(); } catch(err) {}
    function relTimeFor(e) {
      if (t0 === null) return '';
      try {
        var ms = new Date(e.called_at).getTime() - t0;
        return fmtRelTime(ms >= 0 ? ms : 0);
      } catch(err) { return ''; }
    }

    // Swimlane bar: one colored segment per call, width ∝ cost
    var minFlex = total > 0 ? total * 0.01 / events.length : 1;
    var segs = '';
    events.forEach(function(e) {
      var cost = e.cost_dollars || 0;
      var flexVal = total > 0 ? Math.max(cost, minFlex) : 1;
      var rt = relTimeFor(e);
      var tip = '#' + (e.call_sequence || 0) + ' ' + e.tool_name
        + ' ($' + cost.toFixed(4) + ')' + (rt ? ' ' + rt : '');
      segs += '<span class="cr-tl-seg" style="flex:' + flexVal
        + ';background:' + toolColor(e.tool_name) + '" title="'
        + tip.replace(/"/g, '&quot;') + '"></span>';
    });
    var swimlane = '<div class="cr-tl-bar">' + segs + '</div>';

    // Detail table rows
    var rows = '';
    events.forEach(function(e) {
      var cost = e.cost_dollars || 0;
      rows += '<tr class="tc-row">'
        + '<td class="tc-seq">' + (e.call_sequence || 0) + '</td>'
        + '<td class="tc-tool"><span class="cr-tl-dot" style="background:'
        + toolColor(e.tool_name) + '"></span>' + escHtml(e.tool_name) + '</td>'
        + '<td class="tc-cost" style="text-align:right;font-variant-numeric:tabular-nums;">$' + cost.toFixed(4) + '</td>'
        + '<td class="tc-reltime">' + escHtml(relTimeFor(e)) + '</td>'
        + '</tr>\\n';
    });
    return '<details class="cr-tool-panel">'
      + '<summary class="cr-tool-panel-summary">'
      + '<span class="cr-tool-panel-arrow">&#9654;</span>'
      + '<span class="cr-tool-panel-label">Tool timeline</span>'
      + '<span class="cr-tool-panel-count">' + events.length + ' calls</span>'
      + '<span class="cr-tool-panel-total" title="Total cost of individual call events">$' + total.toFixed(4) + '</span>'
      + '</summary>'
      + '<div class="cr-tool-panel-body">'
      + swimlane
      + '<table class="tc-table">'
      + '<thead><tr><th class="tc-seq" style="text-align:right">#</th><th>Tool</th>'
      + '<th style="text-align:right">Cost</th><th class="tc-reltime">Time</th></tr></thead>'
      + '<tbody>' + rows + '</tbody>'
      + '</table></div></details>';
  }

  function renderCriterionToolPanel(toolStats) {
    if (!toolStats || toolStats.length === 0) return '';
    var total = 0;
    toolStats.forEach(function(t) { total += t.total_cost || 0; });
    var rows = '';
    toolStats.forEach(function(t) {
      var cost = t.total_cost || 0;
      var pct = total > 0 ? (cost / total * 100) : 0;
      rows += '<tr class="tc-row">'
        + '<td class="tc-tool">' + escHtml(t.tool_name) + '</td>'
        + '<td class="tc-calls" style="text-align:right;font-variant-numeric:tabular-nums;">' + (t.call_count || 0).toLocaleString() + '</td>'
        + '<td class="tc-cost" style="text-align:right;font-variant-numeric:tabular-nums;">$' + cost.toFixed(4) + '</td>'
        + '<td class="tc-pct" style="min-width:100px;">'
        + '<div style="display:flex;align-items:center;gap:6px;">'
        + '<div style="flex:1;background:var(--border);border-radius:3px;height:8px;overflow:hidden;">'
        + '<div style="width:' + pct.toFixed(1) + '%;background:var(--accent,#3b82f6);height:100%;border-radius:3px;"></div>'
        + '</div>'
        + '<span style="font-size:0.75rem;color:var(--text-muted,#6b7280);min-width:36px;">' + pct.toFixed(1) + '%</span>'
        + '</div></td>'
        + '</tr>\\n';
    });
    return '<details class="cr-tool-panel">'
      + '<summary class="cr-tool-panel-summary">'
      + '<span class="cr-tool-panel-arrow">&#9654;</span>'
      + '<span class="cr-tool-panel-label">Tool cost</span>'
      + '<span class="cr-tool-panel-total" title="Sum of per-tool attributed costs — may differ from criterion cost_dollars">$' + total.toFixed(4) + '</span>'
      + '</summary>'
      + '<div class="cr-tool-panel-body">'
      + '<table class="tc-table">'
      + '<thead><tr><th>Tool</th><th style="text-align:right">Calls</th>'
      + '<th style="text-align:right">Cost</th><th>Share</th></tr></thead>'
      + '<tbody>' + rows + '</tbody>'
      + '</table></div></details>';
  }

  function getProportionalToolStats(cr, taskData) {
    if (cr.tool_stats && cr.tool_stats.length > 0) return cr.tool_stats;
    var taskToolStats = (taskData && taskData.task_tool_stats) || [];
    if (!taskToolStats.length) return [];
    var crCost = cr.cost_dollars || 0;
    var taskTotalCost = (taskData && taskData.task_total_cost) || 0;
    if (crCost <= 0 || taskTotalCost <= 0) return [];
    var frac = crCost / taskTotalCost;
    return taskToolStats.map(function(t) {
      return {
        tool_name: t.tool_name,
        call_count: Math.round((t.call_count || 0) * frac),
        total_cost: (t.total_cost || 0) * frac,
      };
    }).filter(function(t) { return t.total_cost > 0.000001; });
  }

  function renderCriterionItem(cr, repoUrl, taskData) {
    var done = cr.is_completed;
    var css = done ? 'criterion-done' : 'criterion-pending';
    var check = done ? '&#10003;' : '&#9711;';
    var commitBadge = '';
    if (cr.commit_hash) {
      if (repoUrl) {
        commitBadge = '<a href="' + repoUrl + '/commit/' + escHtml(cr.commit_hash) + '" class="criterion-commit" target="_blank">' + escHtml(cr.commit_hash) + '</a>';
      } else {
        commitBadge = '<span class="criterion-commit">' + escHtml(cr.commit_hash) + '</span>';
      }
    }
    var costInline = cr.cost_dollars ? '<span class="criterion-cost">$' + cr.cost_dollars.toFixed(4) + '</span>' : '';

    var toolPanel = (cr.tool_events && cr.tool_events.length > 0)
      ? renderCriterionTimeline(cr.tool_events)
      : renderCriterionToolPanel(getProportionalToolStats(cr, taskData));

    return '<div class="criterion-item ' + css + '" data-sort-completed="' + escHtml(cr.completed_at || '') + '" '
      + 'data-sort-cost="' + (cr.cost_dollars || 0) + '" data-sort-commit="' + escHtml(cr.commit_hash || '') + '" data-cid="' + cr.id + '">'
      + '<div class="criterion-item-row">'
      + '<span class="criterion-id">#' + cr.id + '</span>'
      + '<span class="criterion-status">' + check + '</span>'
      + costInline
      + '<span class="criterion-text">' + escHtml(cr.criterion) + '</span>'
      + (commitBadge ? '<span class="criterion-badges">' + commitBadge + '</span>' : '')
      + '</div>'
      + toolPanel + '</div>';
  }

  function renderGroupHeader(label, labelHtml, done, total, cost) {
    var costBadge = cost ? ' <span class="criteria-group-cost">$' + cost.toFixed(4) + '</span>' : '';
    var pct = total > 0 ? Math.round(done / total * 100) : 0;
    return '<div class="criteria-group-header"><span class="criteria-group-icon">&#9654;</span> '
      + labelHtml + ' &mdash; <span class="criteria-group-count">' + done + '/' + total + ' done</span>'
      + costBadge + '</div>'
      + '<div class="criteria-group-progress"><div class="criteria-group-progress-fill" style="width:' + pct + '%"></div></div>';
  }

  function buildGroup(groupKey, labelHtml, items, repoUrl, taskData) {
    var done = 0, total = items.length, cost = 0;
    items.forEach(function(cr) {
      if (cr.is_completed) done++;
      cost += cr.cost_dollars || 0;
    });
    var allDone = done === total ? ' criteria-group-all-done' : '';
    var html = '<div class="criteria-type-group' + allDone + '" data-group-type="' + escHtml(groupKey) + '">';
    html += renderGroupHeader(groupKey, labelHtml, done, total, cost);
    html += '<div class="criteria-group-items">';
    items.forEach(function(cr) { html += renderCriterionItem(cr, repoUrl, taskData); });
    html += '</div></div>';
    return html;
  }

  function renderByCommit(taskData) {
    var criteria = taskData.criteria;
    var repoUrl = taskData.repo_url || '';
    var groups = {};
    var timestamps = {};
    criteria.forEach(function(cr) {
      var h = cr.commit_hash || null;
      var key = h || '__uncommitted__';
      if (!groups[key]) groups[key] = [];
      groups[key].push(cr);
      if (h && cr.committed_at && !timestamps[key]) timestamps[key] = cr.committed_at;
    });
    var committed = Object.keys(groups).filter(function(k) { return k !== '__uncommitted__'; });
    committed.sort(function(a, b) { return (timestamps[b] || '').localeCompare(timestamps[a] || ''); });
    var order = committed.slice();
    if (groups['__uncommitted__']) order.push('__uncommitted__');

    var html = '';
    order.forEach(function(key) {
      var labelHtml;
      if (key === '__uncommitted__') {
        labelHtml = '<span class="criteria-group-name">Uncommitted</span>';
      } else {
        var short = escHtml(key.substring(0, 8));
        var ts = fmtDate(timestamps[key] || '');
        if (repoUrl) {
          labelHtml = '<a href="' + repoUrl + '/commit/' + escHtml(key) + '" class="criteria-group-commit-link" target="_blank">' + short + '</a>';
        } else {
          labelHtml = '<span class="criteria-group-commit-hash">' + short + '</span>';
        }
        if (ts) labelHtml += ' <span class="criteria-group-time">' + ts + '</span>';
      }
      html += buildGroup(key, labelHtml, groups[key], repoUrl, taskData);
    });
    return html;
  }

  function renderCriteria(detail) {
    var tid = detail.getAttribute('data-tid');
    var taskData = CDATA[tid];
    if (!taskData) return;
    var target = detail.querySelector('.criteria-render-target');
    target.innerHTML = renderByCommit(taskData);
    // Re-apply sort if active
    var activeSort = detail.querySelector('.criteria-sort-btn.sort-asc, .criteria-sort-btn.sort-desc');
    if (activeSort) {
      applyCriteriaSort(detail, activeSort.getAttribute('data-sort-key'),
        activeSort.classList.contains('sort-asc') ? 'asc' : 'desc');
    }
  }

  function applyCriteriaSort(detail, sortKey, dir) {
    function sortItems(container) {
      var items = Array.prototype.slice.call(container.querySelectorAll(':scope > .criterion-item'));
      if (dir === 'none') {
        items.sort(function(a, b) { return parseInt(a.getAttribute('data-cid')) - parseInt(b.getAttribute('data-cid')); });
      } else {
        var attrName = 'data-sort-' + sortKey;
        var isNumeric = (sortKey === 'cost');
        items.sort(function(a, b) {
          var vA = a.getAttribute(attrName) || '';
          var vB = b.getAttribute(attrName) || '';
          var cmp = isNumeric ? ((parseFloat(vA) || 0) - (parseFloat(vB) || 0)) : vA.localeCompare(vB);
          return dir === 'asc' ? cmp : -cmp;
        });
      }
      items.forEach(function(item) { container.appendChild(item); });
    }
    detail.querySelectorAll('.criteria-group-items').forEach(function(gc) { sortItems(gc); });
    var flat = detail.querySelector('.criteria-render-target');
    if (flat && !detail.querySelector('.criteria-type-group')) { sortItems(flat); }
  }

  // Expand/collapse criteria rows — render on first expand
  body.addEventListener('click', function(e) {
    var row = e.target.closest('tr.expandable');
    if (!row) return;
    var tid = row.getAttribute('data-task-id');
    var detail = body.querySelector('tr.criteria-row[data-parent="' + tid + '"]');
    if (!detail) return;
    var isExpanded = row.classList.toggle('expanded');
    detail.style.display = isExpanded ? '' : 'none';
    if (isExpanded && !criteriaRendered[tid]) {
      var cd = detail.querySelector('.criteria-detail');
      if (cd) renderCriteria(cd);
      criteriaRendered[tid] = true;
    }
  });

  // Criteria group header collapse/expand
  document.addEventListener('click', function(e) {
    var header = e.target.closest('.criteria-group-header');
    if (!header) return;
    e.stopPropagation();
    var group = header.closest('.criteria-type-group');
    if (!group) return;
    group.classList.toggle('collapsed');
  });

  // Criteria sort buttons
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.criteria-sort-btn');
    if (!btn) return;
    e.stopPropagation();
    var detail = btn.closest('.criteria-detail');
    if (!detail) return;
    var bar = btn.closest('.criteria-sort-bar');
    var siblings = bar.querySelectorAll('.criteria-sort-btn');
    var wasAsc = btn.classList.contains('sort-asc');
    var wasDesc = btn.classList.contains('sort-desc');

    siblings.forEach(function(s) {
      s.classList.remove('sort-asc', 'sort-desc');
      s.querySelector('.sort-arrow').textContent = '\u25B2';
    });

    var dir;
    if (!wasAsc && !wasDesc) { dir = 'asc'; }
    else if (wasAsc) { dir = 'desc'; }
    else { dir = 'none'; }

    if (dir !== 'none') {
      btn.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      btn.querySelector('.sort-arrow').textContent = dir === 'asc' ? '\u25B2' : '\u25BC';
    }
    applyCriteriaSort(detail, btn.getAttribute('data-sort-key'), dir);
  });

  // Sort headers
  headers.forEach(function(th) {
    th.addEventListener('click', function() {
      var col = parseInt(th.getAttribute('data-col'));
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = true;
      }
      headers.forEach(function(h) {
        h.classList.remove('sort-asc', 'sort-desc');
        h.querySelector('.sort-arrow').textContent = '\u25B2';
      });
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      th.querySelector('.sort-arrow').textContent = sortAsc ? '\u25B2' : '\u25BC';
      applySort();
    });
  });

  // Status filter dropdown
  statusSelect.addEventListener('change', function() {
    statusFilter = statusSelect.value;
    applyFilter();
  });

  // Dropdown filters
  complexitySelect.addEventListener('change', function() {
    complexityFilter = complexitySelect.value;
    applyFilter();
  });

  // Search input
  searchInput.addEventListener('input', function() {
    searchTerm = searchInput.value.toLowerCase();
    applyFilter();
  });

  // Clear all filters
  clearBtn.addEventListener('click', function() {
    clearAllFilters();
  });

  // Page size
  pageSizeEl.addEventListener('change', function() {
    pageSize = parseInt(pageSizeEl.value);
    currentPage = 1;
    pushHashState();
    render();
  });

  // Prev/Next
  prevBtn.addEventListener('click', function() {
    if (currentPage > 1) { currentPage--; pushHashState(); render(); }
  });
  nextBtn.addEventListener('click', function() {
    var maxP = Math.ceil(filtered.length / pageSize);
    if (currentPage < maxP) { currentPage++; pushHashState(); render(); }
  });

  // Restore state from URL hash, then initial render
  var restored = restoreHashState();
  if (restored) {
    syncUIFromState();
    updateFilterBadge();
  }
  applyFilter();
  applySort();

  // Chart.js initialization (graceful fallback if CDN unavailable)
  var costTrendChart = null;
  var costSkillTrendChart = null;
  var currentPeriod = 'weekly';

  function initCharts() {
    if (typeof Chart === 'undefined') return;

    var style = getComputedStyle(document.documentElement);
    function cssVar(name) { return style.getPropertyValue(name).trim(); }
    var textMuted = cssVar('--text-muted') || '#94a3b8';
    var border = cssVar('--border') || '#e2e8f0';
    var periodLabels = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' };

    // --- Task trend chart ---
    if (window.__tuskCostTrend) {
      var trendData = window.__tuskCostTrend;
      var costTrendCanvas = document.getElementById('costTrendChart');

      if (costTrendChart) { costTrendChart.destroy(); costTrendChart = null; }

      var d = trendData[currentPeriod];
      if (d && d.costs.length && costTrendCanvas) {
        var accent = cssVar('--accent') || '#3b82f6';
        var warning = cssVar('--warning') || '#f59e0b';
        costTrendChart = new Chart(costTrendCanvas, {
          type: 'bar',
          data: {
            labels: d.labels,
            datasets: [
              {
                label: periodLabels[currentPeriod] + ' Cost (Tasks)',
                data: d.costs,
                backgroundColor: accent + 'B3',
                borderColor: accent,
                borderWidth: 1,
                borderRadius: 2,
                yAxisID: 'y',
                order: 2
              },
              {
                label: 'Cumulative (Tasks)',
                data: d.cumulative,
                type: 'line',
                borderColor: warning,
                backgroundColor: warning + '33',
                pointBackgroundColor: warning,
                pointBorderColor: cssVar('--bg-panel') || '#ffffff',
                pointBorderWidth: 1.5,
                pointRadius: 3.5,
                borderWidth: 2.5,
                fill: false,
                tension: 0.1,
                yAxisID: 'y1',
                order: 1
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              tooltip: {
                callbacks: {
                  label: function(ctx) {
                    return ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
                  }
                }
              },
              legend: { labels: { color: textMuted, usePointStyle: true, padding: 16 } }
            },
            scales: {
              x: {
                ticks: { color: textMuted, maxRotation: 45, autoSkip: true, maxTicksLimit: 12, font: { size: 11 } },
                grid: { display: false }
              },
              y: {
                position: 'left',
                ticks: {
                  color: textMuted,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(0).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { color: border, borderDash: [3, 3] }
              },
              y1: {
                position: 'right',
                ticks: {
                  color: warning,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(0).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { drawOnChartArea: false }
              }
            }
          }
        });
      }
    }

    // --- Skill trend chart ---
    if (window.__tuskSkillTrend) {
      var skillTrendData = window.__tuskSkillTrend;
      var skillTrendCanvas = document.getElementById('costSkillTrendChart');

      if (costSkillTrendChart) { costSkillTrendChart.destroy(); costSkillTrendChart = null; }

      var sd = skillTrendData[currentPeriod];
      if (sd && sd.costs.length && skillTrendCanvas) {
        var skillAccent = cssVar('--success') || '#22c55e';
        var skillCum = '#8b5cf6';
        costSkillTrendChart = new Chart(skillTrendCanvas, {
          type: 'bar',
          data: {
            labels: sd.labels,
            datasets: [
              {
                label: periodLabels[currentPeriod] + ' Cost (Skills)',
                data: sd.costs,
                backgroundColor: skillAccent + 'B3',
                borderColor: skillAccent,
                borderWidth: 1,
                borderRadius: 2,
                yAxisID: 'y',
                order: 2
              },
              {
                label: 'Cumulative (Skills)',
                data: sd.cumulative,
                type: 'line',
                borderColor: skillCum,
                backgroundColor: skillCum + '33',
                pointBackgroundColor: skillCum,
                pointBorderColor: cssVar('--bg-panel') || '#ffffff',
                pointBorderWidth: 1.5,
                pointRadius: 3.5,
                borderWidth: 2.5,
                fill: false,
                tension: 0.1,
                yAxisID: 'y1',
                order: 1
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              tooltip: {
                callbacks: {
                  label: function(ctx) {
                    return ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(4).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
                  }
                }
              },
              legend: { labels: { color: textMuted, usePointStyle: true, padding: 16 } }
            },
            scales: {
              x: {
                ticks: { color: textMuted, maxRotation: 45, autoSkip: true, maxTicksLimit: 12, font: { size: 11 } },
                grid: { display: false }
              },
              y: {
                position: 'left',
                ticks: {
                  color: textMuted,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(4).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { color: border, borderDash: [3, 3] }
              },
              y1: {
                position: 'right',
                ticks: {
                  color: skillCum,
                  font: { size: 11 },
                  callback: function(v) { return '$' + v.toFixed(4).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
                },
                grid: { drawOnChartArea: false }
              }
            }
          }
        });
      }
    }
  }

  initCharts();

  var costTabs = document.querySelectorAll('#costTrendTabs .cost-tab');
  costTabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      var target = tab.getAttribute('data-tab');
      costTabs.forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      currentPeriod = target;
      initCharts();
    });
  });

  // Theme toggle
  var themeToggle = document.getElementById('themeToggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', function() {
      var html = document.documentElement;
      var current = html.getAttribute('data-theme');
      var next = current === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('tusk-theme', next);
      // Re-render charts with new theme colors
      setTimeout(function() { initCharts(); }, 50);
    });
  }

  // Dependency badge click-to-scroll
  document.addEventListener('click', function(e) {
    var link = e.target.closest('.dep-link');
    if (!link) return;
    e.preventDefault();
    e.stopPropagation();
    var targetId = link.getAttribute('data-target');
    var targetRow = document.querySelector('tr[data-task-id="' + targetId + '"]');
    if (!targetRow) return;
    if (targetRow.style.display === 'none') {
      clearAllFilters();
    }
    targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
    targetRow.classList.add('dep-highlight');
    setTimeout(function() { targetRow.classList.remove('dep-highlight'); }, 2000);
  });

  // --- Tab navigation ---
  var tabBtns = document.querySelectorAll('#tabBar .tab-btn');
  var tabPanels = document.querySelectorAll('.tab-panel');

  function switchTab(tabId) {
    tabBtns.forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-tab') === tabId);
    });
    tabPanels.forEach(function(p) {
      p.classList.toggle('active', p.id === 'tab-' + tabId);
    });
    // Render DAG on first switch to dag tab
    if (tabId === 'dag' && !window.__dagRendered) {
      window.__dagRendered = true;
      renderDag();
    }
  }

  tabBtns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      var tab = btn.getAttribute('data-tab');
      switchTab(tab);
      // Update URL hash with tab parameter
      var hash = window.location.hash.replace(/^#/, '');
      var pairs = hash ? hash.split('&').filter(function(p) { return p.indexOf('tab=') !== 0; }) : [];
      if (tab !== 'dashboard') pairs.unshift('tab=' + tab);
      var newHash = pairs.join('&');
      history.replaceState(null, '', window.location.pathname + (newHash ? '#' + newHash : ''));
    });
  });

  // Restore tab from URL hash
  (function() {
    var hash = window.location.hash.replace(/^#/, '');
    if (!hash) return;
    var pairs = hash.split('&');
    for (var i = 0; i < pairs.length; i++) {
      var kv = pairs[i].split('=');
      if (kv[0] === 'tab' && kv[1]) {
        switchTab(kv[1]);
        return;
      }
    }
  })();

  // --- DAG rendering ---
  var dagRenderCount = 0;

  function renderDag() {
    if (typeof mermaid === 'undefined') return;
    var showDone = document.getElementById('dagShowDone');
    var def = (showDone && showDone.checked) ? window.DAG_MERMAID_ALL : window.DAG_MERMAID_DEFAULT;
    if (!def) return;
    var container = document.getElementById('dagMermaidContainer');
    if (!container) return;
    dagRenderCount++;
    var graphId = 'dagGraph' + dagRenderCount;
    mermaid.render(graphId, def).then(function(result) {
      container.innerHTML = result.svg;
      if (result.bindFunctions) result.bindFunctions(container);
    }).catch(function(err) {
      console.error('Mermaid render error:', err);
      container.innerHTML = '<p style="color:var(--danger);padding:1rem;">Failed to render DAG. Check console for details.</p>';
    });
  }

  // Show Done toggle
  var dagShowDone = document.getElementById('dagShowDone');
  if (dagShowDone) {
    dagShowDone.addEventListener('change', function() {
      renderDag();
    });
  }

  // --- DAG sidebar functions (global for Mermaid click callbacks) ---
  window.dagShowSidebar = function(nodeId) {
    var id = parseInt(nodeId.replace('T', ''), 10);
    var t = (window.DAG_TASK_DATA || {})[id];
    if (!t) return;

    document.getElementById('dagPlaceholder').style.display = 'none';
    var content = document.getElementById('dagSidebarContent');
    content.classList.add('active');
    document.getElementById('dagSbTitle').textContent = '#' + t.id + ': ' + t.summary;

    var statusMap = {'To Do': 'todo', 'In Progress': 'in-progress', 'Done': 'done'};
    var statusClass = 'status-' + (statusMap[t.status] || 'todo');
    var criteria = t.criteria_total > 0 ? t.criteria_done + '/' + t.criteria_total : '\\u2014';

    var m = '';
    m += '<div class="dag-metric"><span class="dag-metric-label">Status</span><span class="dag-metric-value"><span class="status-badge ' + statusClass + '">' + t.status + '</span></span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Priority</span><span class="dag-metric-value">' + (t.priority || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Complexity</span><span class="dag-metric-value">' + (t.complexity || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Domain</span><span class="dag-metric-value">' + (t.domain || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Type</span><span class="dag-metric-value">' + (t.task_type || '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Priority Score</span><span class="dag-metric-value">' + (t.priority_score != null ? t.priority_score : '\\u2014') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Sessions</span><span class="dag-metric-value">' + t.sessions + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Tokens In</span><span class="dag-metric-value">' + t.tokens_in + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Tokens Out</span><span class="dag-metric-value">' + t.tokens_out + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Cost</span><span class="dag-metric-value">' + t.cost + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Duration</span><span class="dag-metric-value">' + t.duration + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Criteria</span><span class="dag-metric-value">' + criteria + '</span></div>';

    if (t.blockers && t.blockers.length > 0) {
      m += '<div style="margin-top:0.75rem;font-weight:700;font-size:0.85rem;">External Blockers</div>';
      for (var i = 0; i < t.blockers.length; i++) {
        var b = t.blockers[i];
        var badge = b.is_resolved
          ? '<span class="dag-blocker-badge dag-blocker-resolved">Resolved</span>'
          : '<span class="dag-blocker-badge dag-blocker-open">Open</span>';
        m += '<div class="dag-blocker-item"><div class="dag-blocker-header">' + badge + ' <span class="dag-blocker-type">' + (b.blocker_type || 'external') + '</span></div><div class="dag-blocker-desc">' + b.description + '</div></div>';
      }
    }

    document.getElementById('dagSbMetrics').innerHTML = m;
  };

  window.dagShowBlockerSidebar = function(nodeId) {
    var id = parseInt(nodeId.replace('B', ''), 10);
    var b = (window.DAG_BLOCKER_DATA || {})[id];
    if (!b) return;

    document.getElementById('dagPlaceholder').style.display = 'none';
    var content = document.getElementById('dagSidebarContent');
    content.classList.add('active');
    document.getElementById('dagSbTitle').textContent = 'Blocker #' + b.id;

    var badge = b.is_resolved
      ? '<span class="dag-blocker-badge dag-blocker-resolved">Resolved</span>'
      : '<span class="dag-blocker-badge dag-blocker-open">Open</span>';

    var m = '<div class="dag-metric"><span class="dag-metric-label">Status</span><span class="dag-metric-value">' + badge + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Type</span><span class="dag-metric-value">' + (b.blocker_type || 'external') + '</span></div>';
    m += '<div class="dag-metric"><span class="dag-metric-label">Blocks Task</span><span class="dag-metric-value">#' + b.task_id + '</span></div>';
    m += '<div style="margin-top:0.75rem;font-size:0.85rem;">' + b.description + '</div>';

    document.getElementById('dagSbMetrics').innerHTML = m;
  };
})();
"""
