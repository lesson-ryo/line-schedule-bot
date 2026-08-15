/**
 * Optional Google Apps Script writer for the teacher song-entry page.
 *
 * Script properties required:
 *   SHEET_ID     - repertoire spreadsheet id
 *   SHEET_GID    - target sheet id (normally 0)
 *   WRITE_SECRET - same value as Render REPERTOIRE_SHEET_WRITE_SECRET
 */
function jsonResponse_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function normalized_(value) {
  return String(value || '').toLowerCase().replace(/\s+/g, '');
}

function youtubeId_(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  try {
    const url = new URL(text);
    const host = String(url.hostname || '').toLowerCase();
    if (host === 'youtu.be') return url.pathname.replace(/^\//, '').split('/')[0];
    if (host === 'youtube.com' || host.endsWith('.youtube.com')) {
      if (url.pathname === '/watch') return url.searchParams.get('v') || '';
      const parts = url.pathname.replace(/^\//, '').split('/');
      if (parts.length >= 2 && ['embed', 'shorts', 'live'].includes(parts[0])) {
        return parts[1];
      }
    }
  } catch (_) {
    return '';
  }
  return '';
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const body = JSON.parse((e.postData && e.postData.contents) || '{}');
    const props = PropertiesService.getScriptProperties();
    const expected = props.getProperty('WRITE_SECRET') || '';
    if (!expected || body.secret !== expected) {
      return jsonResponse_({ok: false, error: 'unauthorized'});
    }

    const spreadsheet = SpreadsheetApp.openById(props.getProperty('SHEET_ID'));
    const gid = Number(props.getProperty('SHEET_GID') || '0');
    const sheet = spreadsheet.getSheets().find(s => s.getSheetId() === gid);
    if (!sheet) return jsonResponse_({ok: false, error: 'sheet not found'});

    const title = String(body.title || '').trim();
    const video = String(body.video || '').trim();
    if (!title) return jsonResponse_({ok: false, error: 'title is required'});

    const lastRow = Math.max(sheet.getLastRow(), 1);
    const columnCount = 8;
    const existing = lastRow > 1
      ? sheet.getRange(2, 1, lastRow - 1, columnCount).getDisplayValues()
      : [];
    const videoId = youtubeId_(video);
    for (const row of existing) {
      if (normalized_(row[3]) === normalized_(title)) {
        return jsonResponse_({ok: false, error: `duplicate title (ID ${row[0]})`});
      }
      const existingVideo = String(row[5] || '').trim();
      if (video && (existingVideo === video || (videoId && youtubeId_(existingVideo) === videoId))) {
        return jsonResponse_({ok: false, error: `duplicate video (ID ${row[0]})`});
      }
    }

    const maxId = existing.reduce((max, row) => Math.max(max, Number(row[0]) || 0), 0);
    const newId = maxId + 1;
    sheet.insertRowAfter(1);
    if (sheet.getLastRow() >= 3) {
      const source = sheet.getRange(3, 1, 1, columnCount);
      const destination = sheet.getRange(2, 1, 1, columnCount);
      source.copyTo(destination, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      source.copyTo(destination, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
    }
    sheet.getRange(2, 1, 1, columnCount).setValues([[
      newId,
      String(body.instrument || '').trim(),
      String(body.kind || '').trim(),
      title,
      String(body.artist || '').trim(),
      video,
      String(body.note || '').trim(),
      String(body.genre || '').trim(),
    ]]);
    SpreadsheetApp.flush();
    return jsonResponse_({ok: true, id: newId});
  } catch (error) {
    return jsonResponse_({ok: false, error: String(error).slice(0, 300)});
  } finally {
    lock.releaseLock();
  }
}
