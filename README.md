# LINE公式アカウント 日程調整Bot（タップ投票版・外部API不要）

LINE公式アカウントの友だち（メンバー）に日程候補をボタン付きメッセージ（Flex Message）で一斉送信し、メンバーがボタンをタップするだけで回答できるBotです。タップは複数選択・解除（もう一度タップ）に対応し、集計は構造化データを読むだけなので外部APIは一切使いません。

想定人数50人程度であれば、以下の構成はすべて無料枠に収まります。

- LINE公式アカウント：フリープラン（月200通まで無料。返信への自動応答はカウント対象外）
- サーバー：Render無料プラン（月750時間まで無料）
- 集計処理：外部APIなし（費用ゼロ）

---

## 全体の流れ

1. LINE Developersで「Messaging APIチャネル」を作成し、認証情報を取得する
2. このコードをGitHubにアップロードする
3. Renderにデプロイし、環境変数を設定する
4. RenderのURLをLINE DevelopersのWebhook URLに設定する
5. メンバーにLINE公式アカウントを友だち追加してもらう
6. `schedule_tools.py` で日程候補を送信・返信を集計する

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

## 2. コードをGitHubにアップロードする

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

## 3. Renderにデプロイする

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
6. 「Create Web Service」でデプロイ開始（数分かかります）
7. デプロイ完了後に表示されるURL（例: `https://your-app.onrender.com`）を控える

> 無料プランはShell（サーバーに直接コマンドを打つ機能）が使えないため、日程候補の送信や集計はShellではなく、次のセクションで説明するブラウザからアクセスできるURLで行います。

---

## 4. Webhook URLを設定する

1. LINE Developers Consoleに戻り、「Messaging API設定」タブを開く
2. 「Webhook URL」に `https://your-app.onrender.com/webhook` を入力して保存
3. 「検証」ボタンを押して成功することを確認
4. 「Webhookの利用」がオンになっていることを再確認

> 無料プランはアクセスがないとスリープするため、Webhook検証時に一度失敗しても、数十秒待ってもう一度「検証」を押すと成功することがあります。

---

## 5. メンバーに友だち追加してもらう

チャネル基本設定タブにあるQRコードや友だち追加URLをメンバーに共有し、LINE公式アカウントを友だち追加してもらってください。追加された時点で `members.json` に自動で記録されます。

---

## 6. 日程候補を送る・投票を集計する（ブラウザから操作）

無料プランではShellが使えないため、以下のURLにブラウザでアクセスするだけで操作します。`YOUR_TOKEN` の部分は手順3で設定した `ADMIN_TOKEN` の値に置き換えてください。このURLは合言葉（トークン）を含むので、他人に共有しないでください。

**登録メンバー一覧を確認する**（送信先を絞りたいときに使う番号を確認）

```
https://your-app.onrender.com/admin/members?token=YOUR_TOKEN
```

**日程候補を送信する**（候補は `|` 区切りで指定。日本語や記号も使えます）

```
https://your-app.onrender.com/admin/send?token=YOUR_TOKEN&candidates=8/5(水) 14:00-|8/6(木) 10:00-|8/7(金) 15:00-
```

全員ではなく特定のメンバーだけに送りたい場合は、`/admin/members` で確認した番号を `&to=` で指定してください（カンマ区切りで複数指定可）。

```
https://your-app.onrender.com/admin/send?token=YOUR_TOKEN&candidates=8/5(水) 14:00-|8/6(木) 10:00-&to=1,3
```

**投票を集計する**（メンバーがボタンをタップした後にアクセス）

```
https://your-app.onrender.com/admin/summarize?token=YOUR_TOKEN
```

**投票データをリセットする**（次回の日程調整の前に）

```
https://your-app.onrender.com/admin/reset?token=YOUR_TOKEN
```

`summarize` にアクセスすると、候補ごとの得票数・回答者名・最多得票の候補が表示されます。タップは構造化データとして届くため、自由記述のような解釈ミスは発生しません。

> 無料プランは15分アクセスがないとスリープするため、初回アクセス時は表示まで30〜60秒ほどかかることがあります。

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `app.py` | LINEからのWebhook（友だち追加・ボタンタップ）を受け取るサーバー本体 |
| `schedule_tools.py` | 日程候補（ボタン付き）の一斉送信・投票の集計を行うコマンドラインツール |
| `requirements.txt` | 必要なPythonライブラリ |
| `.env.example` | 環境変数のサンプル（ローカルで動かす場合に `.env` にコピーして使用） |
| `members.json` | 友だち追加してくれたメンバーの一覧（自動生成） |
| `votes.json` | メンバーがタップした投票の一覧（自動生成） |
| `candidates.json` | 直近で送信した日程候補の一覧（自動生成、集計時に参照） |

---

## 費用の目安（メンバー50人の場合）

| 項目 | 無料枠 | 50人での消費目安 |
|---|---|---|
| LINEメッセージ（Push） | 月200通まで無料 | 日程候補送信1回＝50通消費（月4回程度まで無料） |
| LINEメッセージ（Reply） | カウント対象外・無制限 | 返信への自動応答は何通でも無料 |
| Render | 月750時間無料 | 個人利用なら余裕で収まる |
| 集計処理 | 外部API不使用 | 費用ゼロ |

外部APIを使わないため、月の運用コストは実質ゼロです。
