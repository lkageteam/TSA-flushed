// ─── Configuration ────────────────────────────────────────────────────────────
var MYSQL_HOST     = '75.119.154.255';
var MYSQL_PORT     = '3306';
var MYSQL_USER     = 'root';
var MYSQL_PASSWORD = 'LkaRoot2025Secure!';
var MYSQL_DATABASE = 'lka_tsa_deployments';

var SHEET_SUMMARY = 'Summary (Monthly)';
var SHEET_DAILY   = 'Daily Details';

// Drive folder + monthly report config
var REPORTS_FOLDER_NAME = 'TSA Performance Reports';
var REPORT_NAME_PREFIX  = 'TSA Performance - '; // + YYYY-MM

var MAX_JDBC_ATTEMPTS  = 9;
var JDBC_BASE_DELAY_MS = 3000;
var JDBC_MAX_DELAY_MS  = 20000;

// ─── JDBC helpers ─────────────────────────────────────────────────────────────

function getMySQLConnection() {
  var lastError = null;
  for (var attempt = 1; attempt <= MAX_JDBC_ATTEMPTS; attempt++) {
    try {
      var conn = Jdbc.getConnection(
        'jdbc:mysql://' + MYSQL_HOST + ':' + MYSQL_PORT + '/' + MYSQL_DATABASE,
        MYSQL_USER,
        MYSQL_PASSWORD
      );
      return conn;
    } catch (e) {
      lastError = e;
      console.log('JDBC tentative ' + attempt + '/' + MAX_JDBC_ATTEMPTS + ' échouée : ' + e.message);
      if (attempt === MAX_JDBC_ATTEMPTS) throw lastError;
      var delay = Math.min(JDBC_BASE_DELAY_MS + (attempt - 1) * 3000, JDBC_MAX_DELAY_MS);
      console.log('  Attente ' + delay + 'ms…');
      Utilities.sleep(delay);
    }
  }
}

function queryWithRetry(conn, sql) {
  var lastError = null;
  for (var attempt = 1; attempt <= MAX_JDBC_ATTEMPTS; attempt++) {
    try {
      var stmt = conn.createStatement();
      return stmt.executeQuery(sql);
    } catch (e) {
      lastError = e;
      console.log('Query tentative ' + attempt + '/' + MAX_JDBC_ATTEMPTS + ' échouée : ' + e.message);
      if (attempt === MAX_JDBC_ATTEMPTS) throw lastError;
      var delay = Math.min(JDBC_BASE_DELAY_MS + (attempt - 1) * 2000, 12000);
      Utilities.sleep(delay);
    }
  }
}

// ─── Utility: find Form Responses sheet ───────────────────────────────────────

function getFormResponsesSheet(ss) {
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName().toLowerCase();
    if (name.indexOf('form') !== -1 || name.indexOf('réponses') !== -1 ||
        name.indexOf('reponses') !== -1 || name.indexOf('responses') !== -1) {
      return sheets[i];
    }
  }
  // Fallback: first sheet
  return sheets[0];
}

// ─── Utility: get or create a named sheet ────────────────────────────────────

function getOrCreateSheet(ss, name) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }
  return sheet;
}

// ─── Drive folder + monthly report management ────────────────────────────────

/** Returns (creating if needed) the Drive folder that stores monthly reports. */
function getReportsFolder() {
  var props = PropertiesService.getScriptProperties();
  var folderId = props.getProperty('REPORTS_FOLDER_ID');
  if (folderId) {
    try {
      return DriveApp.getFolderById(folderId);
    } catch (e) {
      console.log('Dossier Drive introuvable (recréation) : ' + e.message);
    }
  }
  var folder;
  var existing = DriveApp.getFoldersByName(REPORTS_FOLDER_NAME);
  if (existing.hasNext()) {
    folder = existing.next();
  } else {
    folder = DriveApp.createFolder(REPORTS_FOLDER_NAME);
    console.log('Dossier Drive créé : ' + REPORTS_FOLDER_NAME);
  }
  props.setProperty('REPORTS_FOLDER_ID', folder.getId());
  return folder;
}

/**
 * Returns the monthly report spreadsheet for monthKey (YYYY-MM).
 * Reuses the existing one (cached id → name search), or creates a new one
 * inside the reports folder.
 */
function getMonthlyReport(monthKey) {
  var reportName = REPORT_NAME_PREFIX + monthKey;
  var props = PropertiesService.getScriptProperties();
  var cacheKey = 'REPORT_ID_' + monthKey;

  var reportId = props.getProperty(cacheKey);
  if (reportId) {
    try {
      return SpreadsheetApp.openById(reportId);
    } catch (e) {
      console.log('Rapport mensuel caché introuvable (recherche) : ' + e.message);
    }
  }

  var folder = getReportsFolder();
  var files = folder.getFilesByName(reportName);
  var ss;
  if (files.hasNext()) {
    ss = SpreadsheetApp.openById(files.next().getId());
  } else {
    ss = SpreadsheetApp.create(reportName);
    var file = DriveApp.getFileById(ss.getId());
    folder.addFile(file);
    DriveApp.getRootFolder().removeFile(file);
    // Remove the default empty "Sheet1" later once our tabs exist
    console.log('Nouveau rapport mensuel créé : ' + reportName);
  }
  props.setProperty(cacheKey, ss.getId());
  return ss;
}

/** Drops the default empty sheet (Sheet1 / Feuille1) if our tabs exist. */
function dropDefaultSheet(ss) {
  var defaults = ['Sheet1', 'Feuille1', 'Feuille 1'];
  for (var i = 0; i < defaults.length; i++) {
    var s = ss.getSheetByName(defaults[i]);
    if (s && ss.getSheets().length > 1) {
      ss.deleteSheet(s);
    }
  }
}

// ─── Date helpers ─────────────────────────────────────────────────────────────

/**
 * Returns the date range for a month. offset 0 = current month, -1 = previous.
 * { start: Date (1st 00:00), end: Date (1st of next month 00:00), key: 'YYYY-MM' }
 */
function getMonthRange(offset) {
  var now = new Date();
  var start = new Date(now.getFullYear(), now.getMonth() + offset, 1, 0, 0, 0);
  var end   = new Date(now.getFullYear(), now.getMonth() + offset + 1, 1, 0, 0, 0);
  var key   = start.getFullYear() + '-' + String(start.getMonth() + 1).padStart(2, '0');
  return { start: start, end: end, key: key };
}

function toDateStr(d) {
  var y = d.getFullYear();
  var m = String(d.getMonth() + 1).padStart(2, '0');
  var day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}

// ─── Read transmission data from Form Responses ───────────────────────────────

/**
 * Returns:
 *   transmissionsByCorpNum  : { corporateNum → count }  (for Summary)
 *   transmissionsByDay      : { "YYYY-MM-DD|corporateNum" → count }  (for Daily)
 */
function readTransmissions(ss, range) {
  var sheet = getFormResponsesSheet(ss);
  var data = sheet.getDataRange().getValues();

  if (data.length < 2) {
    return { byCorpNum: {}, byDay: {} };
  }

  // Find column indices from header row
  var headers = data[0].map(function(h) { return String(h).toLowerCase().trim(); });
  var idxTimestamp = headers.indexOf('timestamp');
  var idxCorpNum = -1;
  for (var i = 0; i < headers.length; i++) {
    var h = headers[i];
    if (h.indexOf('corporate') !== -1 || h.indexOf('num') !== -1 && h.indexOf('corporate') !== -1) {
      idxCorpNum = i;
      break;
    }
  }
  // More specific search for "numéro corporate"
  if (idxCorpNum === -1) {
    for (var i = 0; i < headers.length; i++) {
      if (headers[i].indexOf('corporate') !== -1) {
        idxCorpNum = i;
        break;
      }
    }
  }

  if (idxTimestamp === -1 || idxCorpNum === -1) {
    console.log('WARN: Colonnes Timestamp ou Numéro corporate introuvables dans Form Responses.');
    console.log('Colonnes disponibles : ' + headers.join(', '));
    return { byCorpNum: {}, byDay: {} };
  }

  var byCorpNum = {};
  var byDay = {};

  for (var row = 1; row < data.length; row++) {
    var rawTs = data[row][idxTimestamp];
    if (!rawTs) continue;
    var ts = new Date(rawTs);
    if (ts < range.start || ts >= range.end) continue;

    var corpNum = String(data[row][idxCorpNum]).trim();
    if (!corpNum || corpNum === '' || corpNum === '0' ||
        corpNum.toLowerCase() === 'undefined') continue;

    // Summary count
    byCorpNum[corpNum] = (byCorpNum[corpNum] || 0) + 1;

    // Daily count
    var dayKey = toDateStr(ts) + '|' + corpNum;
    byDay[dayKey] = (byDay[dayKey] || 0) + 1;
  }

  console.log('Transmissions lues : ' + Object.keys(byCorpNum).length + ' TSA uniques.');
  return { byCorpNum: byCorpNum, byDay: byDay };
}

// ─── Read MySQL deployment data ───────────────────────────────────────────────

/**
 * Returns:
 *   deploymentsByCorpNum : { corporateNum → total_count }
 *   deploymentsByDay     : { "YYYY-MM-DD|corporateNum" → count }
 *   tsaInfo              : { corporateNum → { name, region } }
 */
function readMySQLData(range) {
  var startStr = toDateStr(range.start);
  var endStr   = toDateStr(range.end);
  var conn = null;
  try {
    conn = getMySQLConnection();

    // Query 1: deployments summary (month range)
    var sqlDeplSummary =
      "SELECT corporate_num, tsa_full_name, region, SUM(deployment_count) AS total " +
      "FROM deployments_daily " +
      "WHERE deployment_date >= '" + startStr + "' AND deployment_date < '" + endStr + "' " +
      "GROUP BY corporate_num, tsa_full_name, region";

    var rsDeplSummary = queryWithRetry(conn, sqlDeplSummary);
    var deploymentsByCorpNum = {};
    var tsaInfo = {};
    while (rsDeplSummary.next()) {
      var corpNum = rsDeplSummary.getString('corporate_num');
      deploymentsByCorpNum[corpNum] = parseInt(rsDeplSummary.getString('total')) || 0;
      tsaInfo[corpNum] = {
        name:   rsDeplSummary.getString('tsa_full_name') || '',
        region: rsDeplSummary.getString('region') || ''
      };
    }
    rsDeplSummary.close();

    // Query 2: deployments daily detail (month range)
    var sqlDeplDaily =
      "SELECT deployment_date, corporate_num, deployment_count " +
      "FROM deployments_daily " +
      "WHERE deployment_date >= '" + startStr + "' AND deployment_date < '" + endStr + "' " +
      "ORDER BY deployment_date ASC";

    var rsDeplDaily = queryWithRetry(conn, sqlDeplDaily);
    var deploymentsByDay = {};
    while (rsDeplDaily.next()) {
      var dateStr = rsDeplDaily.getString('deployment_date').substring(0, 10);
      var corp    = rsDeplDaily.getString('corporate_num');
      var cnt     = parseInt(rsDeplDaily.getString('deployment_count')) || 0;
      var key = dateStr + '|' + corp;
      deploymentsByDay[key] = (deploymentsByDay[key] || 0) + cnt;
    }
    rsDeplDaily.close();

    // Query 3: full tsa_reference for TSA names (includes TSA without deployments yet)
    var sqlTsaRef = "SELECT corporate_num, tsa_full_name, region FROM tsa_reference";
    var rsTsaRef = queryWithRetry(conn, sqlTsaRef);
    while (rsTsaRef.next()) {
      var corp = rsTsaRef.getString('corporate_num');
      if (!tsaInfo[corp]) {
        tsaInfo[corp] = {
          name:   rsTsaRef.getString('tsa_full_name') || '',
          region: rsTsaRef.getString('region') || ''
        };
      }
    }
    rsTsaRef.close();

    console.log(
      'MySQL lu : ' + Object.keys(deploymentsByCorpNum).length + ' TSA avec déploiements, ' +
      Object.keys(tsaInfo).length + ' TSA total.'
    );
    return {
      deploymentsByCorpNum: deploymentsByCorpNum,
      deploymentsByDay:     deploymentsByDay,
      tsaInfo:              tsaInfo
    };

  } finally {
    if (conn) conn.close();
  }
}

// ─── Format helpers ───────────────────────────────────────────────────────────

/**
 * Applies clean styling to a freshly-written sheet.
 *   numRows     : total rows including header
 *   numCols     : number of columns
 *   numericCols : array of 1-based column indices to center (numbers)
 *   widths      : array of {col, width} for fixed column widths
 *   updatedAt   : Date for the "last updated" note on A1
 */
function styleSheet(sheet, numRows, numCols, numericCols, widths, updatedAt) {
  // Remove any leftover bandings (avoids accumulation across runs)
  var bandings = sheet.getBandings();
  for (var b = 0; b < bandings.length; b++) {
    bandings[b].remove();
  }

  // Base font on the whole used range
  var used = sheet.getRange(1, 1, Math.max(numRows, 1), numCols);
  used.setFontFamily('Arial').setFontSize(10).setVerticalAlignment('middle');

  // Header row
  var header = sheet.getRange(1, 1, 1, numCols);
  header.setFontWeight('bold')
        .setBackground('#1a73e8')
        .setFontColor('#ffffff')
        .setHorizontalAlignment('center');
  sheet.setRowHeight(1, 30);
  sheet.setFrozenRows(1);

  // Data area: borders + alternating banding
  if (numRows > 1) {
    var dataRange = sheet.getRange(2, 1, numRows - 1, numCols);
    dataRange.applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);
    used.setBorder(true, true, true, true, true, true,
                   '#dddddd', SpreadsheetApp.BorderStyle.SOLID);
  }

  // Numeric columns centered
  if (numericCols && numRows > 1) {
    for (var i = 0; i < numericCols.length; i++) {
      sheet.getRange(2, numericCols[i], numRows - 1, 1).setHorizontalAlignment('center');
    }
  }

  // Fixed column widths
  if (widths) {
    for (var w = 0; w < widths.length; w++) {
      sheet.setColumnWidth(widths[w].col, widths[w].width);
    }
  }

  // "Last updated" note on A1 (clean, no overflow)
  if (updatedAt) {
    sheet.getRange(1, 1).setNote('Mis à jour : ' + updatedAt.toLocaleString('fr-FR'));
  }
}

// ─── Write Summary (Monthly) tab ──────────────────────────────────────────────

function writeSummary(reportSS, transmissions, mysqlData, updatedAt) {
  var byCorpNum     = transmissions.byCorpNum;
  var deplByCorpNum = mysqlData.deploymentsByCorpNum;
  var tsaInfo       = mysqlData.tsaInfo;

  // Display only TSA present in the reference (tsaInfo) → no phantom rows.
  // Sort by deployments (descending), then by name (ascending).
  var rows = [['TSA', 'Region', 'Transmissions', 'Deployments done']];

  var corpNums = Object.keys(tsaInfo).sort(function(a, b) {
    var deplA = deplByCorpNum[a] || 0;
    var deplB = deplByCorpNum[b] || 0;
    if (deplB !== deplA) return deplB - deplA; // desc deployments
    var nameA = tsaInfo[a].name || a;
    var nameB = tsaInfo[b].name || b;
    return nameA.localeCompare(nameB); // asc name as tie-breaker
  });

  for (var i = 0; i < corpNums.length; i++) {
    var corp = corpNums[i];
    var info = tsaInfo[corp];
    rows.push([
      info.name || corp,
      info.region || '',
      byCorpNum[corp]     || 0,
      deplByCorpNum[corp] || 0
    ]);
  }

  var sheet = getOrCreateSheet(reportSS, SHEET_SUMMARY);
  sheet.clear();
  sheet.getRange(1, 1, rows.length, 4).setValues(rows);

  styleSheet(
    sheet, rows.length, 4,
    [3, 4],
    [{ col: 1, width: 260 }, { col: 2, width: 120 },
     { col: 3, width: 120 }, { col: 4, width: 150 }],
    updatedAt
  );

  console.log('[Summary] ' + (rows.length - 1) + ' TSA écrits.');
}

// ─── Write Daily Details tab ──────────────────────────────────────────────────

function writeDaily(reportSS, transmissions, mysqlData, updatedAt) {
  var byDay     = transmissions.byDay;
  var deplByDay = mysqlData.deploymentsByDay;
  var tsaInfo   = mysqlData.tsaInfo;

  // Union of all (date|corpNum) keys, skipping corp not in reference.
  var allKeys = {};
  Object.keys(byDay).forEach(function(k)     { allKeys[k] = true; });
  Object.keys(deplByDay).forEach(function(k) { allKeys[k] = true; });

  var rows = [['Date', 'TSA', 'Region', 'Transmissions', 'Deployments']];

  // Build array of objects for sorting by deployments (descending)
  var rowObjs = [];
  Object.keys(allKeys).forEach(function(key) {
    var parts = key.split('|');
    var dateStr = parts[0];
    var corp    = parts[1];
    var info    = tsaInfo[corp];
    if (!info) return; // skip unknown corp (phantom)
    var depl = deplByDay[key] || 0;
    var trans = byDay[key] || 0;
    rowObjs.push({
      date: dateStr,
      name: info.name || corp,
      region: info.region || '',
      trans: trans,
      depl: depl,
      sortKey: -depl // negative for descending sort
    });
  });

  rowObjs.sort(function(a, b) {
    if (a.sortKey !== b.sortKey) return a.sortKey - b.sortKey;
    return a.date.localeCompare(b.date); // tie-breaker: date ascending
  });

  rowObjs.forEach(function(obj) {
    rows.push([obj.date, obj.name, obj.region, obj.trans, obj.depl]);
  });

  var sheet = getOrCreateSheet(reportSS, SHEET_DAILY);
  sheet.clear();
  sheet.getRange(1, 1, rows.length, 5).setValues(rows);

  styleSheet(
    sheet, rows.length, 5,
    [4, 5],
    [{ col: 1, width: 110 }, { col: 2, width: 260 }, { col: 3, width: 120 },
     { col: 4, width: 120 }, { col: 5, width: 120 }],
    updatedAt
  );

  console.log('[Daily] ' + (rows.length - 1) + ' lignes écrites.');
}

// ─── Report orchestrator ──────────────────────────────────────────────────────

/**
 * Builds (or refreshes) the monthly report for the given offset.
 *   offset 0  = current month
 *   offset -1 = previous month (finalization)
 * Reads transmissions from the bound Form Responses sheet and deployments
 * from MySQL, then writes the Summary + Daily tabs into the month's dedicated
 * spreadsheet stored in the Drive reports folder.
 */
function buildReport(offset) {
  var range  = getMonthRange(offset);
  var source = SpreadsheetApp.getActiveSpreadsheet();
  var now    = new Date();

  var transmissions = readTransmissions(source, range);
  var mysqlData     = readMySQLData(range);

  var reportSS = getMonthlyReport(range.key);
  writeSummary(reportSS, transmissions, mysqlData, now);
  writeDaily(reportSS, transmissions, mysqlData, now);
  buildDashboard(reportSS);
  dropDefaultSheet(reportSS);

  console.log('[Report ' + range.key + '] OK → ' + reportSS.getUrl());
  return reportSS;
}

// ─── Dashboard chart sheet ───────────────────────────────────────────────────

var SHEET_DASHBOARD    = 'Dashboard';
var CHART_COLS_PER_ROW = 3;   // how many month charts per grid row
var CHART_WIDTH        = 420; // px per chart
var CHART_HEIGHT       = 280; // px per chart
var CHART_H_GAP        = 20;  // horizontal gap between charts
var CHART_V_GAP        = 30;  // vertical gap between rows
var CHART_TOP_OFFSET   = 60;  // px reserved for dashboard header row

/**
 * Builds or refreshes the Dashboard sheet inside `reportSS`.
 * Reads the Daily Details tab for daily totals and draws one combo chart
 * per month (bar = deployments, line = transmissions), laid out in a grid.
 *
 * The chart for a given month is always placed at a fixed grid position
 * (col = monthIndex % CHART_COLS_PER_ROW, row = floor(monthIndex / CHART_COLS_PER_ROW))
 * so charts never shuffle when a new month starts.
 */
function buildDashboard(reportSS) {
  var dailySheet = reportSS.getSheetByName(SHEET_DAILY);
  if (!dailySheet) {
    console.log('[Dashboard] Daily Details introuvable — skip.');
    return;
  }

  var data = dailySheet.getDataRange().getValues();
  if (data.length < 2) {
    console.log('[Dashboard] Pas encore de données.');
    return;
  }

  // Parse daily data → { 'YYYY-MM' → { 'YYYY-MM-DD' → { trans, depl } } }
  // Group by calendar month (YYYY-MM), not by day name
  var monthData = {};
  for (var r = 1; r < data.length; r++) {
    var rawDate = data[r][0];
    // Handle both Date objects and strings
    var dateObj;
    if (rawDate instanceof Date) {
      dateObj = rawDate;
    } else {
      dateObj = new Date(String(rawDate));
    }
    if (isNaN(dateObj.getTime())) continue;

    // Format: YYYY-MM-DD for the day, YYYY-MM for the month key
    var yyyy = dateObj.getFullYear();
    var mm = String(dateObj.getMonth() + 1).padStart(2, '0');
    var dd = String(dateObj.getDate()).padStart(2, '0');
    var dateStr = yyyy + '-' + mm + '-' + dd;     // YYYY-MM-DD
    var monthKey = yyyy + '-' + mm;                // YYYY-MM

    var trans = Number(data[r][3]) || 0;
    var depl  = Number(data[r][4]) || 0;

    if (!monthData[monthKey]) monthData[monthKey] = {};
    if (!monthData[monthKey][dateStr]) monthData[monthKey][dateStr] = { trans: 0, depl: 0 };
    monthData[monthKey][dateStr].trans += trans;
    monthData[monthKey][dateStr].depl  += depl;
  }

  var months = Object.keys(monthData).sort();
  if (months.length === 0) return;

  var dash = getOrCreateSheet(reportSS, SHEET_DASHBOARD);
  dash.clear();

  // Header row on the sheet (row 1, cols A-D used as label area)
  var now = new Date();
  dash.getRange(1, 1).setValue('TSA Performance — Dashboard')
      .setFontWeight('bold').setFontSize(14);
  dash.getRange(1, 1).setNote('Mis à jour : ' + now.toLocaleString('fr-FR'));
  dash.getRange(1, 1, 1, 6).merge()
      .setBackground('#1a73e8').setFontColor('#ffffff').setHorizontalAlignment('center');
  dash.setRowHeight(1, 40);

  // Remove old charts
  var oldCharts = dash.getCharts();
  for (var c = 0; c < oldCharts.length; c++) {
    dash.removeChart(oldCharts[c]);
  }

  // For each month: write a mini data table starting at a hidden area (row 200+)
  // then build a chart anchored at the correct grid position.
  var DATA_START_ROW = 200; // hidden below visible area

  for (var mi = 0; mi < months.length; mi++) {
    var monthKey = months[mi];
    var days = Object.keys(monthData[monthKey]).sort();

    // Write mini table: col offset = mi * 4 (Date | Trans | Depl | [spacer])
    var colOffset = mi * 4 + 1; // 1-based
    var tableStartRow = DATA_START_ROW;

    // Header
    dash.getRange(tableStartRow, colOffset, 1, 3)
        .setValues([['Date', 'Transmissions', 'Deployments']]);

    // Data rows
    var tableData = days.map(function(d) {
      return [d, monthData[monthKey][d].trans, monthData[monthKey][d].depl];
    });
    if (tableData.length > 0) {
      dash.getRange(tableStartRow + 1, colOffset, tableData.length, 3)
          .setValues(tableData);
    }

    // Chart anchor position (grid)
    var gridCol = mi % CHART_COLS_PER_ROW;
    var gridRow = Math.floor(mi / CHART_COLS_PER_ROW);
    var anchorX  = gridCol * (CHART_WIDTH + CHART_H_GAP) + 10;
    var anchorY  = CHART_TOP_OFFSET + gridRow * (CHART_HEIGHT + CHART_V_GAP);

    var dataRange = dash.getRange(
      tableStartRow, colOffset,
      tableData.length + 1, 3
    );

    var chart = dash.newChart()
      .setChartType(Charts.ChartType.COMBO)
      .addRange(dataRange)
      .setNumHeaders(1)
      .setOption('title', monthKey)
      .setOption('titleTextStyle', { bold: true, fontSize: 12 })
      .setOption('series', {
        0: { type: 'bars',  color: '#4a90d9', targetAxisIndex: 0 },
        1: { type: 'line',  color: '#e67e22', targetAxisIndex: 1,
             lineWidth: 2, pointSize: 4 }
      })
      .setOption('vAxes', {
        0: { title: 'Deployments', minValue: 0 },
        1: { title: 'Transmissions', minValue: 0 }
      })
      .setOption('hAxis', { slantedText: true, slantedTextAngle: 45 })
      .setOption('legend', { position: 'bottom' })
      .setOption('backgroundColor', '#ffffff')
      .setOption('chartArea', { left: 55, top: 40, width: '75%', height: '60%' })
      .setPosition(1, 1, anchorX, anchorY)
      .setOption('width',  CHART_WIDTH)
      .setOption('height', CHART_HEIGHT)
      .build();

    dash.insertChart(chart);
    console.log('[Dashboard] Graphique ' + monthKey + ' positionné (' + gridCol + ',' + gridRow + ').');
  }

  // Move Dashboard tab to first position for visibility
  reportSS.setActiveSheet(dash);
  reportSS.moveActiveSheet(1);

  console.log('[Dashboard] ' + months.length + ' graphique(s) générés.');
}

// ─── Manual helpers ───────────────────────────────────────────────────────────

function updateCurrentMonth()    { return buildReport(0);  }
function finalizePreviousMonth() { return buildReport(-1); }

// ─── Main entry point (called by trigger) ────────────────────────────────────

function runUpdate() {
  try {
    // Always refresh the current month (auto-creates a new spreadsheet when
    // the month rolls over).
    buildReport(0);

    // During the first 3 days of a new month, finalize the previous month's
    // report so late-arriving data is captured before we stop touching it.
    var now = new Date();
    if (now.getDate() <= 3) {
      buildReport(-1);
      console.log('[Finalize] Rapport du mois précédent finalisé.');
    }

    console.log('[DONE] Mise à jour terminée avec succès.');
  } catch (e) {
    console.error('[ERROR] runUpdate failed: ' + e.message + '\n' + e.stack);
    // Optionally send email on error
    // MailApp.sendEmail('your@email.com', 'TSA Sheet Error', e.message + '\n' + e.stack);
  }
}

// ─── Trigger management ───────────────────────────────────────────────────────

function createHourlyTrigger() {
  // Delete existing triggers for runUpdate to avoid duplicates
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'runUpdate') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  // Create new hourly trigger
  ScriptApp.newTrigger('runUpdate')
    .timeBased()
    .everyHours(1)
    .create();
  console.log('Trigger horaire créé pour runUpdate().');
}

function deleteAllTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    ScriptApp.deleteTrigger(triggers[i]);
  }
  console.log('Tous les triggers supprimés.');
}

/** Logs the URL of the current month's report (creates it if missing). */
function logCurrentReportUrl() {
  var range = getMonthRange(0);
  var ss = getMonthlyReport(range.key);
  console.log('Rapport ' + range.key + ' : ' + ss.getUrl());
  return ss.getUrl();
}

// ─── MySQL connectivity test ──────────────────────────────────────────────────

function testMySQLConnection() {
  var maxAttempts = 5;
  var baseDelayMs = 2000;
  var lastError = null;

  for (var attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      console.log('Tentative ' + attempt + '/' + maxAttempts + '…');
      var conn = Jdbc.getConnection(
        'jdbc:mysql://' + MYSQL_HOST + ':' + MYSQL_PORT,
        MYSQL_USER,
        MYSQL_PASSWORD
      );

      var stmt = conn.createStatement();
      var rs = stmt.executeQuery('SELECT VERSION() as version, NOW() as server_time');
      var version = '';
      var serverTime = '';
      if (rs.next()) {
        version    = rs.getString('version');
        serverTime = rs.getString('server_time');
      }
      rs.close();

      var dbList = [];
      var rs2 = stmt.executeQuery('SHOW DATABASES');
      while (rs2.next()) {
        var dbName = rs2.getString(1);
        if (['information_schema', 'performance_schema', 'mysql', 'sys'].indexOf(dbName) === -1) {
          dbList.push(dbName);
        }
      }
      rs2.close();

      console.log('✅ Connexion MySQL OK !');
      console.log('Version : ' + version);
      console.log('Heure serveur : ' + serverTime);
      console.log('Bases disponibles : ' + dbList.join(', '));

      try {
        var ss = SpreadsheetApp.getActiveSpreadsheet();
        if (ss) {
          ss.toast('MySQL OK! Bases: ' + dbList.join(', '), 'Test connexion', 10);
        }
      } catch (toastErr) { /* ignore si pas de contexte sheet */ }

      stmt.close();
      conn.close();
      return true;

    } catch (e) {
      lastError = e;
      console.log('❌ Tentative ' + attempt + ' échouée : ' + e.message);
      if (attempt === maxAttempts) {
        console.log('Toutes les tentatives ont échoué. Dernière erreur : ' + lastError.message);
        try {
          SpreadsheetApp.getActiveSpreadsheet().toast(
            'MySQL ECHEC : ' + lastError.message, 'Test connexion', 30
          );
        } catch (toastErr) { /* ignore */ }
        return false;
      }
      var delay = Math.min(baseDelayMs + (attempt - 1) * 2000, 12000);
      console.log('Attente ' + delay + 'ms avant retry…');
      Utilities.sleep(delay);
    }
  }
}
