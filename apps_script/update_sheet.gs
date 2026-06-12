// ─── Configuration ────────────────────────────────────────────────────────────
var MYSQL_HOST     = '75.119.154.255';
var MYSQL_PORT     = '3306';
var MYSQL_USER     = 'root';
var MYSQL_PASSWORD = 'LkaRoot2025Secure!';
var MYSQL_DATABASE = 'lka_tsa_deployments';

var SHEET_SUMMARY = 'Summary (Monthly)';
var SHEET_DAILY   = 'Daily Details';

var MAX_JDBC_ATTEMPTS  = 5;
var JDBC_BASE_DELAY_MS = 2000;

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
      var delay = Math.min(JDBC_BASE_DELAY_MS + (attempt - 1) * 2000, 12000);
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

// ─── Date helpers ─────────────────────────────────────────────────────────────

function getMonthStart() {
  var now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0);
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
function readTransmissions(ss) {
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

  var monthStart = getMonthStart();
  var now = new Date();

  var byCorpNum = {};
  var byDay = {};

  for (var row = 1; row < data.length; row++) {
    var rawTs = data[row][idxTimestamp];
    if (!rawTs) continue;
    var ts = new Date(rawTs);
    if (ts < monthStart || ts > now) continue;

    var corpNum = String(data[row][idxCorpNum]).trim();
    if (!corpNum || corpNum === '' || corpNum.toLowerCase() === 'undefined') continue;

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
function readMySQLData() {
  var conn = null;
  try {
    conn = getMySQLConnection();

    // Query 1: deployments summary (current month)
    var sqlDeplSummary =
      "SELECT corporate_num, tsa_full_name, region, SUM(deployment_count) AS total " +
      "FROM deployments_daily " +
      "WHERE deployment_date >= DATE_FORMAT(NOW(), '%Y-%m-01') " +
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

    // Query 2: deployments daily detail (current month)
    var sqlDeplDaily =
      "SELECT deployment_date, corporate_num, deployment_count " +
      "FROM deployments_daily " +
      "WHERE deployment_date >= DATE_FORMAT(NOW(), '%Y-%m-01') " +
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

function formatHeaders(sheet, numCols) {
  var headerRange = sheet.getRange(1, 1, 1, numCols);
  headerRange.setFontWeight('bold');
  headerRange.setBackground('#1a73e8');
  headerRange.setFontColor('#ffffff');
  headerRange.setHorizontalAlignment('center');
  sheet.setFrozenRows(1);
}

function autoResizeColumns(sheet, numCols) {
  for (var i = 1; i <= numCols; i++) {
    sheet.autoResizeColumn(i);
  }
}

// ─── Update Summary (Monthly) tab ─────────────────────────────────────────────

function updateSummarySheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var transmissions = readTransmissions(ss);
  var mysqlData = readMySQLData();

  var byCorpNum    = transmissions.byCorpNum;
  var deplByCorpNum = mysqlData.deploymentsByCorpNum;
  var tsaInfo      = mysqlData.tsaInfo;

  // Build union of all corporate numbers
  var allCorpNums = {};
  Object.keys(byCorpNum).forEach(function(k)    { allCorpNums[k] = true; });
  Object.keys(deplByCorpNum).forEach(function(k) { allCorpNums[k] = true; });
  Object.keys(tsaInfo).forEach(function(k)       { allCorpNums[k] = true; });

  var rows = [['TSA', 'Region', 'Transmissions', 'Deployments done']];

  var sortedCorpNums = Object.keys(allCorpNums).sort(function(a, b) {
    var nameA = (tsaInfo[a] && tsaInfo[a].name) ? tsaInfo[a].name : a;
    var nameB = (tsaInfo[b] && tsaInfo[b].name) ? tsaInfo[b].name : b;
    return nameA.localeCompare(nameB);
  });

  for (var i = 0; i < sortedCorpNums.length; i++) {
    var corp = sortedCorpNums[i];
    var info = tsaInfo[corp] || { name: corp, region: '' };
    rows.push([
      info.name || corp,
      info.region || '',
      byCorpNum[corp]    || 0,
      deplByCorpNum[corp] || 0
    ]);
  }

  var sheet = getOrCreateSheet(ss, SHEET_SUMMARY);
  sheet.clearContents();
  if (rows.length > 0) {
    sheet.getRange(1, 1, rows.length, 4).setValues(rows);
  }
  formatHeaders(sheet, 4);
  autoResizeColumns(sheet, 4);

  // Update timestamp in cell F1
  sheet.getRange(1, 6).setValue('Mis à jour : ' + new Date().toLocaleString('fr-FR'));

  console.log('[Summary] ' + (rows.length - 1) + ' TSA écrits.');
}

// ─── Update Daily Details tab ─────────────────────────────────────────────────

function updateDailyDetailsSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var transmissions = readTransmissions(ss);
  var mysqlData = readMySQLData();

  var byDay    = transmissions.byDay;
  var deplByDay = mysqlData.deploymentsByDay;
  var tsaInfo  = mysqlData.tsaInfo;

  // Build union of all (date|corpNum) keys
  var allKeys = {};
  Object.keys(byDay).forEach(function(k)    { allKeys[k] = true; });
  Object.keys(deplByDay).forEach(function(k) { allKeys[k] = true; });

  var rows = [['Date', 'TSA', 'Region', 'Transmissions', 'Deployments']];

  var sortedKeys = Object.keys(allKeys).sort();

  for (var i = 0; i < sortedKeys.length; i++) {
    var key = sortedKeys[i];
    var parts = key.split('|');
    var dateStr = parts[0];
    var corp    = parts[1];
    var info    = tsaInfo[corp] || { name: corp, region: '' };
    rows.push([
      dateStr,
      info.name || corp,
      info.region || '',
      byDay[key]    || 0,
      deplByDay[key] || 0
    ]);
  }

  var sheet = getOrCreateSheet(ss, SHEET_DAILY);
  sheet.clearContents();
  if (rows.length > 0) {
    sheet.getRange(1, 1, rows.length, 5).setValues(rows);
  }
  formatHeaders(sheet, 5);
  autoResizeColumns(sheet, 5);

  // Update timestamp in cell G1
  sheet.getRange(1, 7).setValue('Mis à jour : ' + new Date().toLocaleString('fr-FR'));

  console.log('[Daily] ' + (rows.length - 1) + ' lignes écrites.');
}

// ─── Monthly reset ────────────────────────────────────────────────────────────

function clearPreviousMonthData() {
  var now = new Date();
  if (now.getDate() !== 1) return;

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var summary = ss.getSheetByName(SHEET_SUMMARY);
  var daily   = ss.getSheetByName(SHEET_DAILY);

  if (summary && summary.getLastRow() > 1) {
    summary.getRange(2, 1, summary.getLastRow() - 1, summary.getLastColumn()).clearContent();
    console.log('[Reset] Summary vidé.');
  }
  if (daily && daily.getLastRow() > 1) {
    daily.getRange(2, 1, daily.getLastRow() - 1, daily.getLastColumn()).clearContent();
    console.log('[Reset] Daily vidé.');
  }
}

// ─── Main entry point (called by trigger) ────────────────────────────────────

function runUpdate() {
  try {
    clearPreviousMonthData();
    updateSummarySheet();
    updateDailyDetailsSheet();
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
