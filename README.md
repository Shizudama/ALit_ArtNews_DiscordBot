# Discord Scout Bot

Bluesky と RSS から指定キーワードを含む「影響力のある投稿/記事」を収集して Discord に通知するボット。
GitHub Actions の無料枠で動作する。

## 動作概要

- **Bluesky**: 公開API (`public.api.bsky.app`) でキーワード検索 → いいね/リポスト数で閾値判定
- **RSS**: feedparser で取得 → タイトル/概要にキーワードがあれば通知
- **Discord**: Webhook で `embeds` を送信
- **重複防止**: `state.json` に既送信IDを記録し、リポジトリに push して保持

## セットアップ手順

### 1. Discord Webhook の作成

通知を流したいDiscordサーバーで:
1. チャンネル設定 → 連携サービス → ウェブフック → 新しいウェブフック
2. URL をコピー(後で使う)

### 2. GitHub リポジトリ作成

1. このディレクトリ一式を空のリポジトリに push する。
2. **公開リポジトリ推奨**(GitHub Actions の実行分数が無制限のため)。
   - 公開でも秘密情報はSecretsに置けば露出しない。
   - 非公開にするなら月2,000分の制限内で運用(30分間隔なら余裕)。

### 3. Secrets の設定

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | 1.でコピーしたURL |

### 4. 動作確認

**Settings → Actions → General** で workflow の権限が `Read and write permissions` になっていることを確認。

**Actions タブ → scout → Run workflow** で手動実行してテスト。
ログでヒット件数を確認、Discord に通知が届けばOK。

## チューニング

### キーワードの閾値

`config.yaml` の `keywords` で個別に調整。

```yaml
- { word: "メディアアート", like_threshold: 5, repost_threshold: 2 }
```

- `like_threshold` か `repost_threshold` の **どちらか** を満たせば通知。
- 一般語(「アート」「AI」「本」)はノイズが多いので閾値を高めに。
- 中心テーマ(「メディアアート」「文学」)は低めにして拾いやすく。

複数キーワードがマッチした場合は **最も低い閾値** が採用される(より緩く判定される)。

### RSSフィードの追加

`config.yaml` の `rss_feeds` に URL を追加するだけ。
個人ブログ・noteもRSSが提供されている。

```yaml
rss_feeds:
  - https://note.com/<username>/rss
```

### 実行頻度

`.github/workflows/scout.yml` の cron。デフォルト30分間隔。

- GitHub Actionsの cron は最短5分間隔だが、定期実行は遅延しやすい(数十分の遅延・スキップあり)
- 緊急性が必要なら、外部のcron-job.orgから `workflow_dispatch` API を叩く方式が確実

### Bluesky の言語フィルタ

`bluesky_lang: ja` で日本語のみに限定。
英語投稿も拾いたいなら `bluesky_lang: null` (またはコメントアウト)。

## 拡張アイデア

- **Mastodon追加**: `Mastodon.py` で `/api/v2/search` を叩く。同様に無料・無認証で使える(インスタンス次第)
- **発信者ホワイトリスト**: 特定アカウントの投稿は閾値を緩和、または無条件で通知
- **発信者ブラックリスト**: bot系・スパム系を除外
- **X(Twitter)が必要なら**: rss.app など外部サービスで特定アカウントをRSS化し、`rss_feeds` に追加(完全無料ではないが安価)

## トラブルシュート

- **`state.json` の commit 失敗** → workflow の `permissions: contents: write` を確認
- **Bluesky検索が0件** → `since` 形式が正しいか、キーワードが普通に存在するかをcurlで確認:
  ```
  curl 'https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=メディアアート&limit=10'
  ```
- **長期間動かないリポジトリ** → 60日コミットがないとscheduleが自動停止する仕様。state.jsonの更新で自然に避けられる。

## ファイル構成

```
.
├── bot.py                       # メイン処理
├── config.yaml                  # キーワード/閾値/RSS定義
├── requirements.txt             # 依存パッケージ
├── state.json                   # 重複防止用の既送信ID(自動更新)
├── README.md
└── .github/workflows/scout.yml  # cron実行
```
