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

function ensureSchema_(sheet) {
  const columnCount = 9;
  if (sheet.getMaxColumns() < columnCount) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), columnCount - sheet.getMaxColumns());
  }
  if (sheet.getRange(1, 9).getDisplayValue() !== '公開状態') {
    sheet.getRange(1, 9).setValue('公開状態');
    if (sheet.getLastColumn() >= 8) {
      sheet.getRange(1, 8).copyTo(sheet.getRange(1, 9), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      sheet.getRange(1, 9).setValue('公開状態');
    }
  }
  const rows = Math.max(sheet.getMaxRows() - 1, 1);
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(['公開', '非公開'], true)
    .setAllowInvalid(false)
    .build();
  sheet.getRange(2, 9, rows, 1).setDataValidation(rule);
  return columnCount;
}

function calendarForTenant_(props, tenant) {
  if (!['kansai', 'kanto'].includes(tenant)) {
    throw new Error('invalid tenant');
  }
  const propertyName = tenant.toUpperCase() + '_CALENDAR_ID';
  const configuredId = props.getProperty(propertyName) || props.getProperty('CALENDAR_ID') || '';
  let calendar = null;
  if (configuredId === 'primary') {
    calendar = CalendarApp.getDefaultCalendar();
  } else if (configuredId) {
    calendar = CalendarApp.getCalendarById(configuredId);
  }
  if (!calendar) {
    const label = tenant === 'kansai' ? '関西' : '関東';
    calendar = CalendarApp.createCalendar('Lesson ' + label + ' 日程', {
      description: 'Lesson日程調整アプリから自動同期される予定',
      timeZone: 'Asia/Tokyo',
    });
    props.setProperty(propertyName, calendar.getId());
  }
  return calendar;
}

function authorizeCalendar() {
  // Run once from the editor after adding Calendar support to an old deployment.
  return CalendarApp.getDefaultCalendar().getName();
}

function eventById_(calendar, eventId) {
  if (!eventId) return null;
  try {
    return calendar.getEventById(String(eventId));
  } catch (_) {
    return null;
  }
}

function eventByTag_(calendar, row) {
  const start = new Date(row.start);
  const end = new Date(row.end);
  if (isNaN(start.getTime()) || isNaN(end.getTime()) || start >= end) {
    throw new Error('invalid event time');
  }
  const margin = 24 * 60 * 60 * 1000;
  const events = calendar.getEvents(
    new Date(start.getTime() - margin),
    new Date(end.getTime() + margin)
  );
  return events.find(event => event.getTag('lesson_sync_key') === String(row.key || '')) || null;
}

function syncCalendar_(body, props) {
  const tenant = String(body.tenant || '');
  const rows = Array.isArray(body.events) ? body.events : [];
  const existing = Array.isArray(body.existing) ? body.existing : [];
  const deleteEventIds = Array.isArray(body.delete_event_ids) ? body.delete_event_ids : [];
  if (rows.length > 300 || deleteEventIds.length > 300) {
    throw new Error('too many calendar events');
  }
  const calendar = calendarForTenant_(props, tenant);
  const oldByKey = {};
  existing.forEach(row => {
    if (row && row.key) oldByKey[String(row.key)] = row;
  });

  let created = 0;
  let updated = 0;
  let deleted = 0;
  const records = [];
  rows.forEach(row => {
    const key = String((row && row.key) || '');
    if (!key || key.length > 80) throw new Error('invalid calendar key');
    const start = new Date(row.start);
    const end = new Date(row.end);
    if (isNaN(start.getTime()) || isNaN(end.getTime()) || start >= end) {
      throw new Error('invalid event time');
    }
    const previous = oldByKey[key] || {};
    let event = eventById_(calendar, previous.event_id) || eventByTag_(calendar, row);
    if (event) {
      event
        .setTitle(String(row.title || 'レッスン'))
        .setTime(start, end)
        .setDescription(String(row.description || ''))
        .setLocation(String(row.location || ''));
      updated += 1;
    } else {
      event = calendar.createEvent(String(row.title || 'レッスン'), start, end, {
        description: String(row.description || ''),
        location: String(row.location || ''),
      });
      created += 1;
    }
    event.setTag('lesson_sync_key', key);
    event.setTag('lesson_sync_tenant', tenant);
    records.push({
      key: key,
      event_id: event.getId(),
      start: String(row.start),
      end: String(row.end),
      title: String(row.title || ''),
    });
  });

  const desiredIds = new Set(records.map(row => row.event_id));
  deleteEventIds.forEach(eventId => {
    if (!eventId || desiredIds.has(String(eventId))) return;
    const event = eventById_(calendar, eventId);
    if (event && event.getTag('lesson_sync_tenant') === tenant) {
      event.deleteEvent();
      deleted += 1;
    }
  });
  return {
    ok: true,
    calendar_id: calendar.getId(),
    calendar_name: calendar.getName(),
    records: records,
    created: created,
    updated: updated,
    deleted: deleted,
  };
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

    const action = String(body.action || 'add');
    if (action === 'calendar_sync') {
      return jsonResponse_(syncCalendar_(body, props));
    }
    if (!['add', 'update', 'archive', 'publish'].includes(action)) {
      return jsonResponse_({ok: false, error: 'invalid action'});
    }
    const spreadsheet = SpreadsheetApp.openById(props.getProperty('SHEET_ID'));
    const gid = Number(props.getProperty('SHEET_GID') || '0');
    const sheet = spreadsheet.getSheets().find(s => s.getSheetId() === gid);
    if (!sheet) return jsonResponse_({ok: false, error: 'sheet not found'});
    const columnCount = ensureSchema_(sheet);
    const lastRow = Math.max(sheet.getLastRow(), 1);
    const existing = lastRow > 1
      ? sheet.getRange(2, 1, lastRow - 1, columnCount).getDisplayValues()
      : [];

    const requestedId = Number(body.id || 0);
    const targetIndex = existing.findIndex(row => Number(row[0]) === requestedId);
    if (action !== 'add' && targetIndex < 0) {
      return jsonResponse_({ok: false, error: 'song not found'});
    }
    if (action === 'archive' || action === 'publish') {
      sheet.getRange(targetIndex + 2, 9).setValue(action === 'archive' ? '非公開' : '公開');
      SpreadsheetApp.flush();
      return jsonResponse_({ok: true, id: requestedId, active: action === 'publish'});
    }

    const title = String(body.title || '').trim();
    const video = String(body.video || '').trim();
    if (!title) return jsonResponse_({ok: false, error: 'title is required'});
    const videoId = youtubeId_(video);
    for (const row of existing) {
      if (action === 'update' && Number(row[0]) === requestedId) continue;
      if (normalized_(row[3]) === normalized_(title)) {
        return jsonResponse_({ok: false, error: `duplicate title (ID ${row[0]})`});
      }
      const existingVideo = String(row[5] || '').trim();
      if (video && (existingVideo === video || (videoId && youtubeId_(existingVideo) === videoId))) {
        return jsonResponse_({ok: false, error: `duplicate video (ID ${row[0]})`});
      }
    }

    let targetRow;
    let materialId;
    if (action === 'add') {
      const maxId = existing.reduce((max, row) => Math.max(max, Number(row[0]) || 0), 0);
      materialId = maxId + 1;
      sheet.insertRowAfter(1);
      targetRow = 2;
      if (sheet.getLastRow() >= 3) {
        const source = sheet.getRange(3, 1, 1, columnCount);
        const destination = sheet.getRange(2, 1, 1, columnCount);
        source.copyTo(destination, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
        source.copyTo(destination, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
      }
    } else {
      materialId = requestedId;
      targetRow = targetIndex + 2;
    }
    sheet.getRange(targetRow, 1, 1, columnCount).setValues([[
      materialId,
      String(body.instrument || '').trim(),
      String(body.kind || '').trim(),
      title,
      String(body.artist || '').trim(),
      video,
      String(body.note || '').trim(),
      String(body.genre || '').trim(),
      existing[targetIndex] && existing[targetIndex][8] === '非公開' ? '非公開' : '公開',
    ]]);
    SpreadsheetApp.flush();
    return jsonResponse_({ok: true, id: materialId});
  } catch (error) {
    return jsonResponse_({ok: false, error: String(error).slice(0, 300)});
  } finally {
    lock.releaseLock();
  }
}
