# Blocking Shell

長時間のビルド・テストを1回のMCP呼び出しで終了まで待つCodex Plugin。
`omit_tools_from: ["code_mode"]` により直接呼び出しを使う。
サーバーは`systemd-run --user`のoneshotサービスの終了を非同期で待ち、
ポーリング用ハンドルを返さない。unitを保持して統計を取得してから停止・回収する。

必要環境はLinux、systemd 254以上の稼働中user manager、Bash、uv、Python 3.11以上。
呼び出し側にはuser busへの接続環境が必要。MCP SDKとtiktokenはuvが導入する。初回起動時にtiktokenが辞書をダウンロード・キャッシュする。
GitHubからインストールする（pluginコマンドに対応したCodexが必要）:

```sh
codex plugin marketplace add skmdx/blocking-shell
codex plugin add blocking-shell@blocking-shell
```

インストール後、新しいスレッドで利用する。
ソースを変更して試す場合は、cloneしたリポジトリの絶対パスを
`codex plugin marketplace add /absolute/path/to/blocking-shell` に指定する。
`.mcp.json`はインストール先を基準にサーバーを起動するため、固定の配置パスは不要。

`run` の引数・出力・実行権限は [スキル](plugins/blocking-shell/skills/blocking-shell/SKILL.md) と
ツールの説明を参照。ホスト側のツール期限は24時間より120秒長く設定し、
コマンドの期限超過時に終了処理と結果の返却を行う余裕を設けている。
`cmd`にコマンド、`workdir`に既存ディレクトリの絶対パスを指定する。
`workdir`は`$BLOCKING_SHELL_SCRATCH_DIR`または`${BLOCKING_SHELL_SCRATCH_DIR}`でも指定でき、
末尾に`/subdir`を付けられる。毎回その実行の`scratch_ref`から解決するため、
`rerun(scratch_ref="...")`でも新しい保存先に追従する。他の変数やシェル式は展開しない。
MCPにはターンのcwdが渡されないため、`workdir`は必須。
`rerun()`は同じ会話の直前に受け付けたコマンドを、同じ実行条件で再実行する。
環境変数は現在の設定を使う。ログは毎回新規作成し、Scratchの保存先を変更する場合だけ
`rerun(scratch_ref="...")`を指定する。履歴はMCP再接続・圧縮後も維持する。
Scratchプラグインの`create`が返す英単語IDを`scratch_ref`へ渡す。
コマンド内では`"$BLOCKING_SHELL_SCRATCH_DIR"`でそのディレクトリの絶対パスを参照できる。
`run`・`rerun`ごとに設定し、継承環境や`set_env`の同名設定より優先する。
例: `make > "$BLOCKING_SHELL_SCRATCH_DIR/build.log"`。
`timeout_seconds`は省略時も21600秒（6時間）で停止する。
別の実行期限が必要な場合だけ1〜86400秒で指定する。期限では子孫プロセスも停止する。
保存結果の`timeout_seconds`で実際に適用した期限を確認できる。
全子孫プロセスを含むメモリ上限は既定8 GiB。`memory_max_mib`で正のMiB値を指定できる。
swapは禁止し、メモリ超過時はcgroup全体を停止する。上限を外した自動再試行はしない。
適用値は保存結果の`memory_max_bytes`と`memory_swap_max_bytes`で確認できる。
`shell`省略時はユーザーの既定シェルを使う。特定のシェル構文が必要な場合だけ指定する。
`login=true`（既定）は`-lc`でログイン時の起動ファイルを読み込む。
起動ファイルによる環境変更を避ける場合は`login=false`とし、`-c`で実行する。
標準入力は閉じ、stdout/stderrを結合ログへ保存する。PTY・対話入力は提供しない。
`max_output_tokens`はログ末尾だけの上限で既定1000。結果JSONのメタデータは対象外。
通常は終了状態・終了コード・経過時間・CPU/メモリ/IO統計・ログ末尾・切詰め有無と、
`log_path`・`result_path`を返す。失敗時はunit情報とsystemdログへの参照も返す。
完全なstdout/stderrは結合ログ、詳細な結果は`result_path`のJSONに保存する。
保存結果には適用した制限、ログ全体のバイト数、末尾の`output_tokens`・`output_token_encoding`も含む。
計数はtiktokenの`o200k_base`を使い、不正なUTF-8は置換する。利用モデルの課金トークン数ではない。
`set_env(values={"CC": "clang"})`で、この会話の以後の`run`へ渡す環境変数を設定する。
`list_env()`はツールで設定した名前と値だけを返す。
`unset_env(names=["CC"])`は登録した上書きを削除し、継承値があればそれに戻す。
設定は会話IDごとに`$XDG_STATE_HOME/blocking-shell`（既定`~/.local/state/blocking-shell`）へ保存し、
MCP再接続後も維持する。保存先はホスト環境の`BLOCKING_SHELL_STATE_DIR`で上書きできる。
他の会話・他のツール・実行中のコマンド・systemdの管理操作には適用しない。
一回だけの指定は`CC=clang make`のように`cmd`内へ書く。
転送する基底変数は`.mcp.json`の`env_vars`で指定し、別シェルでの変更は自動継承しない。
自動コンテキスト圧縮後は、`SessionStart`フックが設定した名前と値をモデルへ注入する。
導入時に`/hooks`でフックを確認・信頼する。Codex 0.162.0の手動圧縮APIでは発火しない。
CPU時間は秒、最大メモリとブロックIO量はバイトで、子孫を含むunit cgroupの回収前の値。
取得不能は`null`となる。統計取得・回収に失敗した場合だけ`accounting_error`・`cleanup_error`を返す。
サーバーの強制終了やホスト障害からのジョブ復元は行わない。

結果が不要になったらScratchの`delete(refs=[...])`で一時ディレクトリごと削除する。
実行中は参照をロックし、削除は`skipped_active`となる。削除失敗分は再試行できる。
共通クライアント`scratch_space.py`はworkspaceの`tools/scratch/sync.py`で同期する生成物。

実MCP通信の試験:

```sh
uv run --script plugins/blocking-shell/tests/test_mcp.py /absolute/scratch
TMPDIR=/absolute/scratch uv run --script plugins/blocking-shell/tests/test_environment.py
TMPDIR=/absolute/scratch uv run --script plugins/blocking-shell/tests/test_rerun.py
```

インストール済みPluginを実Codexで確認する試験（Codex利用枠を使用）:

```sh
python3 plugins/blocking-shell/tests/smoke_codex.py --codex /path/to/codex --out-dir /absolute/new-scratch
```

65秒待機を含むMakefileでCプログラムをコンパイル・実行し、Codexの実記録で
直接呼び出し1回、途中のポーリング0回、正常終了、実行結果を検証する。
呼び出しで期限を上書きせず、既定21600秒が適用されることも確認する。
試験の出力ディレクトリは確認後に削除する。

## テンプレート候補の実験

`experiments/template_similarity.py` はsession-historyの取得JSONを入力に、
直近100回の`run`を最大50件前まで比較するオフラインPoC。
raw DEFLATEの対称化NCDと、シェルトークン列のSequenceMatcherを比較する。
履歴内のコマンドは実行せず、MCPサーバーやインストール済みPluginも変更しない。

```sh
uv run --script experiments/template_similarity.py history.json --out result.json
```

資源制限付きのblocking-shellから実行する。前半で閾値を選び、後半を評価する。
評価ラベルは「pytest呼び出しが出力先だけ異なり、実行条件も一致する」という限定した基準。
完全一致のコマンドは精度評価から除外し、長すぎる入力は切り詰めず除外数を返す。
一般的な意味の類似性や、別セッションへの汎化精度を測るものではない。

候補が3件以上集まると、異なる`--option=/path`だけを値へ分離できる場合に
テンプレート案を生成し、元の文字列への完全な復元を検証する。
これはレビュー用の案であり、シェル構文の安全な生成・実行機能ではない。
入力トークン比較は`o200k_base`、空白なしJSON、定義と説明を1回分含めた回顧的な試算。
ツールスキーマ・応答・会話履歴の再入力は含まず、実現済みの節約量とは区別する。

結果JSONは比較に使ったコマンドと実行条件を含むため、公開前の確認が必要。
全会話の代わりにこの結果をローカルへ保持し、同じ選択上限で入力に指定して再評価できる。

MIT License。詳細は [LICENSE](LICENSE) を参照。
