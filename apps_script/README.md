# Google Sheet song writer and Calendar sync

The teacher song-entry page works immediately with shared Redis storage. To
add, edit, archive, and republish songs directly in the repertoire Google Sheet,
deploy `Code.gs` as a
Google Apps Script web app and set these Render environment variables:

- `REPERTOIRE_SHEET_WRITE_URL`
- `REPERTOIRE_SHEET_WRITE_SECRET`

Set matching Apps Script properties:

- `SHEET_ID`
- `SHEET_GID` (`0` for the current repertoire tab)
- `WRITE_SECRET`

Deploy the web app to execute as the owner and allow anyone with the URL. The
shared secret is checked on every write request.

The same web app also synchronizes confirmed lesson schedules to Google
Calendar. On the first sync it automatically creates two separate calendars:

- `Lesson 関西 日程`
- `Lesson 関東 日程`

Their IDs are saved automatically as the script properties
`KANSAI_CALENDAR_ID` and `KANTO_CALENDAR_ID`. To use calendars that already
exist, set those properties to the corresponding Calendar IDs before the first
sync. To intentionally put both regions on one calendar, set `CALENDAR_ID`
instead.

When Calendar support is added to an existing deployment, authorize the new
Calendar permission and create a new web-app version. The teacher page at
`/admin/calendar` can retry a failed sync safely. Only events created by this
app are updated or deleted; manually entered Calendar events are untouched.

The repertoire sheet must use these columns in order:

`ID / 楽器 / 形態 / 曲名 / アーティスト / Youtube 演奏動画 / メモ / ジャンル / 公開状態`

New songs are inserted directly below the header. Formatting and data
validation are copied from the previous first song row before values are set.
The first write safely adds the `公開状態` column when it is missing and applies
the `公開 / 非公開` dropdown. Archiving never deletes or renumbers a song, so old
carte progress remains linked to the same ID.
