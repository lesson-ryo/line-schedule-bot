# Google Sheet song writer

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

The repertoire sheet must use these columns in order:

`ID / 楽器 / 形態 / 曲名 / アーティスト / Youtube 演奏動画 / メモ / ジャンル / 公開状態`

New songs are inserted directly below the header. Formatting and data
validation are copied from the previous first song row before values are set.
The first write safely adds the `公開状態` column when it is missing and applies
the `公開 / 非公開` dropdown. Archiving never deletes or renumbers a song, so old
carte progress remains linked to the same ID.
