# LINE公式アカウント 日程調整Bot（LIFFチェックボックス投票版）

LINE公式アカウントの友だち（メンバー）に日程候補を送ると、メンバーはLINEアプリ内で開くミニWebページ（LIFF）上でチェックボックスから複数選択して回答できるBotです。候補数に上限は実質なく（LINEのFlex Messageの50KB制限内であればOK）、集計は構造化データを読むだけなので自由記述の解釈ミスもありません。

想定人数50人程度であれば、以下の構成はすべて無料枠に収まります。

- LINE公式アカウント：フリープラン（月200通まで無料。返信への自動応答はカウント対象外）
- サーバー：Render無料プラン（月750時間まで無料）
- データ保存：Upstash Redis無料枠（Renderのスリープ・再デプロイでもデータが消えない）
- 集計処理：外部APIなし（費用ゼロ）

---

## 全体の流れ

1. LINE Developersで「Messaging APIチャネル」を作成し、認証情報を取得する
2. Upstash（無料）でデータ保存用のRedisを作成する
3. このコードをGitHubにアップロードする
4. Renderにデプロイし、環境変数を設定する
5. RenderのURLをLINE DevelopersのWebhook URLに設定する
6. LINE DevelopersでLIFFアプリを登録する（回答フォーム用）
7. メンバーにLINE公式アカウントを友だち追加してもらう
8. `schedule_tools.py` で日程候補を送信・返信を集計する

---

## 1. LINE Developersでチャネルを作成する

1. [LINE Developers Console](https://developers.line.biz/console/) にログイン
2. 「プロバイダー」を新規作成（すでにあれば選択）
3. 「新規チャネル作成」→「Messaging API」を選択
4. チャネル名・説明などを入力して作成
5. 作成したチャネルの「Messaging API設定」タブを開き、以下を控える
   - **チャネルアクセストークン**（長期）→ 発行ボタンを押して取得
   - **チャネルシークレット**（「チャネル基本設定」タブに表示）
6. 同じ画面で以下を設定
   - 「応答メッセージ」→ **オフ**（Botのデフォルト応答と競合しないように）
   - 「Webhookの利用」→ **オン**
   - 「あいさつメッセージ」は任意でオフにしてOK

---

## 2. Upstashでデータ保存用のRedisを作成する

Render無料プランはディスクを保持しないため（15分操作がないとスリープし、再起動・再デプロイのたびにファイルが消える）、友だち一覧や投票データはUpstash（無料のRedis）に保存します。

1. [Upstash](https://upstash.com/) にアクセスし、アカウントを作成（GitHubアカウントでの登録も可）してログイン
2. コンソールで「Create Database」
3. 名前を入力（例: `line-schedule-bot`）、Type は **Regional**、リージョンは日本に近い場所（例: `ap-northeast-1` 東京）を選んで作成
4. 作成したデータベースの詳細画面を開き、「REST API」セクションから以下を控える
   - **UPSTASH_REDIS_REST_URL**
   - **UPSTASH_REDIS_REST_TOKEN**

無料枠は1日1万コマンド程度まで使えるため、友だち50人規模の個人利用であれば十分収まります。

---

## 3. コードをGitHubにアップロードする

このフォルダ（`line-schedule-bot`）の中身をそのまま新しいGitHubリポジトリ（`line-schedule-bot`）にpushしてください。`.env` は `.gitignore` に含まれているので、誤ってアップロードされません。

```bash
cd line-schedule-bot
git init
git add .
git commit -m "initial commit"
# GitHubで空のリポジトリ line-schedule-bot を作成してから
git remote add origin <あなたのリポジトリURL>
git push -u origin main
```

---

## 4. Renderにデプロイする

1. [Render](https://render.com/) にアカウント登録（GitHubアカウントで連携可能）
2. ダッシュボードで「New +」→「Web Service」
3. 先ほどpushしたGitHubリポジトリ（`line-schedule-bot`）を選択
4. 設定は以下の通り
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free
5. 「Environment」タブで環境変数を追加
   - `LINE_CHANNEL_ACCESS_TOKEN` … 手順1で控えたトークン
   - `LINE_CHANNEL_SECRET` … 手順1で控えたシークレット
   - `ADMIN_TOKEN` … 好きな文字列（推測されにくいランダムな文字列。「Generate」ボタンで自動生成できます）。日程送信・集計をブラウザから操作するための合言葉として使います
   - `UPSTASH_REDIS_REST_URL` … 手順2で控えた値
   - `UPSTASH_REDIS_REST_TOKEN` … 手順2で控えた値
6. 「Create Web Service」でデプロイ開始（数分かかります）
7. デプロイ完了後に表示されるURL（例: `https://your-app.onrender.com`）を控える

> 無料プランはShell（サーバーに直接コマンドを打つ機能）が使えないため、日程候補の送信や集計はShellではなく、次のセクションで説明するブラウザからアクセスできるURLで行います。

---

## 5. Webhook URLを設定する

1. LINE Developers Consoleに戻り、「Messaging API設定」タブを開く
2. 「Webhook URL」に `https://your-app.onrender.com/webhook` を入力して保存
3. 「検証」ボタンを押して成功することを確認
4. 「Webhookの利用」がオンになっていることを再確認

> 無料プランはアクセスがないとスリープするため、Webhook検証時に一度失敗しても、数十秒待ってもう一度「検証」を押すと成功することがあります。

---

## 6. LIFFアプリを登録する（回答フォーム用）

回答フォームはLINEアプリ内で開くミニWebページ（LIFF）です。LIFFアプリはMessaging APIチャネルには直接追加できないため、同じプロバイダー内に「LINEログインチャネル」を新規作成し、そちらにLIFFアプリを登録します。

1. LINE Developers Consoleでプロバイダーを開き、「新規チャネル作成」→「LINEログイン」を選択して作成（メールアドレスなど必須項目を入力し、LINE開発者契約に同意）
2. 作成したチャネルの「LIFF」タブ →「追加」
3. 以下を入力
   - **LIFFアプリ名**: 任意（例: 日程調整フォーム）
   - **サイズ**: Tall または Full
   - **エンドポイントURL**: `https://your-app.onrender.com/liff`
   - **Scope**: `openid` と `profile` の両方にチェック
   - **友だち追加オプション**: Off でOK（すでにMessaging API側の公式アカウントを友だち追加してもらう前提のため）
4. 作成後に表示される **LIFF ID**（`1234567890-AbCdEfGh` のような形式）を控える
5. このLINEログインチャネルの「チャネル基本設定」タブを開き、**Channel ID**（チャネルID、数字のみ）を控える
6. Renderの「Environment」タブで環境変数を追加
   - `LIFF_ID` … 手順4で控えたLIFF ID
   - `LINE_CHANNEL_ID` … 手順5で控えたChannel ID
7. 「Save, rebuild, and deploy」で再デプロイ

> LINEログインチャネルとMessaging APIチャネルが同じプロバイダー内にあれば、LIFFで取得できるユーザーIDとMessaging APIのユーザーIDは同じ値になります（提供元が同じであれば自動的に一致します）。

---

## 7. メンバーに友だち追加してもらう

チャネル基本設定タブにあるQRコードや友だち追加URLをメンバーに共有し、LINE公式アカウントを友だち追加してもらってください。追加された時点でUpstash（`members`キー）に自動で記録されます。

---

## 8. 日程候補を送る・投票を集計する（ブラウザから操作）

無料プランではShellが使えないため、以下のURLにブラウザでアクセスするだけで操作します。`YOUR_TOKEN` の部分は手順4で設定した `ADMIN_TOKEN` の値に置き換えてください。このURLは合言葉（トークン）を含むので、他人に共有しないでください。

**登録メンバー一覧を確認する**（送信先を絞りたいときに使う番号を確認）

```
https://your-app.onrender.com/admin/members?token=YOUR_TOKEN
```

**日程候補を送信する**（候補は `|` 区切りで指定。日本語や記号も使えます。件数の上限は実質ありません）

```
https://your-app.onrender.com/admin/send?token=YOUR_TOKEN&candidates=8/5(水) 14:00-|8/6(木) 10:00-|8/7(金) 15:00-
```

全員ではなく特定のメンバーだけに送りたい場合は、`/admin/members` で確認した番号を `&to=` で指定してください（カンマ区切りで複数指定可）。

```
https://your-app.onrender.com/admin/send?token=YOUR_TOKEN&candidates=8/5(水) 14:00-|8/6(木) 10:00-&to=1,3
```

メンバーには「日程を選ぶ」ボタン付きメッセージが届き、タップするとLIFFフォームが開いてチェックボックスで複数選択→送信できます。

**投票を集計する**（メンバーが送信した後にアクセス）

```
https://your-app.onrender.com/admin/summarize?token=YOUR_TOKEN
```

**投票データをリセットする**（次回の日程調整の前に）

```
https://your-app.onrender.com/admin/reset?token=YOUR_TOKEN
```

`summarize` にアクセスすると、候補ごとの得票数・回答者名・最多得票の候補が表示されます。

> 無料プランは15分アクセスがないとスリープするため、初回アクセス時は表示まで30〜60秒ほどかかることがあります（データ自体はUpstashに保存されているので、スリープしても消えません）。

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `app.py` | LINEからのWebhook（友だち追加・LIFF投票受信）を受け取るサーバー本体 |
| `schedule_tools.py` | 日程候補の一斉送信・投票の集計を行うコマンドラインツール |
| `storage.py` | データ保存の共通層。Upstash Redis（本番）またはローカルJSON（開発用フォールバック）を切り替える |
| `requirements.txt` | 必要なPythonライブラリ |
| `.env.example` | 環境変数のサンプル（ローカルで動かす場合に `.env` にコピーして使用） |

友だち一覧（`members`）・投票（`votes`）・直近の候補（`candidates`）は、`UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` が設定されていればUpstashに、未設定であればローカルの `members.json` / `votes.json` / `candidates.json` に保存されます（本番のRenderでは前者を必ず設定してください）。

---

## 費用の目安（メンバー50人の場合）

| 項目 | 無料枠 | 50人での消費目安 |
|---|---|---|
| LINEメッセージ（Push） | 月200通まで無料 | 日程候補送信1回＝50通消費（月4回程度まで無料） |
| LINEメッセージ（Reply） | カウント対象外・無制限 | 返信への自動応答は何通でも無料 |
| Render | 月750時間無料 | 個人利用なら余裕で収まる |
| Upstash Redis | 1日1万コマンドまで無料 | 個人利用なら余裕で収まる |
| 集計処理 | 外部API不使用 | 費用ゼロ |

外部APIを使わないため、月の運用コストは実質ゼロです。
