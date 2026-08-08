# 開発規約

## 公開文面

- このリポジトリは、選択したMeshの接続済みシェーダーをPBRテクスチャへベイクし、標準GLB 2.0へ書き出す汎用Blenderアドオンとして扱う。
- コード、コメント、Docstring、README、テスト名、コミット文面には、特定製品、特定読込先、特定業務用途を記載しない。
- アドオン名は「Shader Bake GLB Exporter」、Sidebar名は「GLB Bake Export」とする。
- 識別子は英語にする。公開API、制約、元データを変更しない理由には日本語コメントまたはDocstringを付ける。

## 実装規約

- 元Object、Mesh、Material、Node、Image、UV、Modifier、Scene設定、Render設定を直接変更しない。
- 一時データはJobが所有権を記録し、成功、失敗、キャンセルの全経路で削除する。
- GLBは一時ファイルへ出力し、構造検証に成功した場合だけ最終パスへ原子的に置換する。
- Blender付属Pythonと`bpy`だけを使用し、独自3D形式や独自GLB writerを追加しない。
- テスト生成物と配布ZIPは再生成可能なartifactとしてGit管理しない。

## 検証規約

- Blender 5.1.1の`--background --factory-startup --disable-autoexec`で自動テストを実行する。
- コミット前に`git diff --check`、Extension検証、ZIP内容検証を実行する。
- 実装済み、未実装、ソフトウェア検証済み、視覚確認済みを混同しない。
