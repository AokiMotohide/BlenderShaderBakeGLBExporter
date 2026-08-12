# Shader Bake GLB Exporter v1.0.0

選択したMeshの接続済みシェーダーをPBRテクスチャへベイクし、標準GLB 2.0として書き出すBlender Extensionです。この更新では材質互換性、Alpha、保存操作を拡張しました。

## ダウンロードとインストール

1. このReleaseのAssetsから`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。
2. ZIPを展開せず、Blender 5.1.1の`Edit > Preferences > Extensions`を開きます。
3. 右上のメニューから`Install from Disk`を選び、ZIPを指定します。
4. 「Shader Bake GLB Exporter」を有効にします。

詳しい操作方法は[日本語マニュアル](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/blob/v1.0.0/docs/USER_MANUAL.ja.md)を参照してください。

## 主な機能

- 選択したMeshだけをGLBへ出力
- 選択Mesh間の階層と配置に必要なEmpty祖先をglTF Nodeとして保持
- 選択Meshが生成するParticle等の静的Mesh instanceを親Node配下へ展開し、ベイク済みMesh定義を共有
- Base Color + Alpha、ORM、Normal、Emissiveを8bit PNGへベイク
- 全texture slotを単一Bake UVと`TEXCOORD_0`へ統一
- 定数Base Color、Alpha、Metallic、Roughness、Emissive、IORを標準factorとして保持
- 連続Alpha、Alpha Blend、Alpha Hashed相当を`alphaMode: BLEND`で出力
- Alpha Clipを`alphaMode: MASK`と`alphaCutoff`で保持
- Transmission、IOR、Specular、Clearcoat、Sheen、Anisotropy、Volume、Emissive Strength、Unlit用KHR拡張を出力
- Mix Shader、Glass、Toon、Subsurface、Thin Film、視線依存表現などをCore PBRへ近似
- Material未割当、Node未使用、Surface未接続、個別Bake失敗を安全なPBR材質へフォールバック
- 近似・置換内容をJob失敗と分けて「警告一覧」へ表示
- Blender標準保存ダイアログ、初期GLB名、`.glb`自動付与、前回保存先の再利用
- 元データを変更せず、成功・失敗・キャンセル時に一時データを削除
- 検証済みGLBだけを最終保存先へ原子的に反映

## 対応環境

- Windows
- Blender 5.1.1
- Cycles

## 制約

外観近似した材質は、元Shaderとの物理的・視線依存な完全一致を保証しません。instanceは現在フレームの静的配置として出力します。Animation、Skin、Morph、Camera、Light、Draco、gltfpack、Custom Propertiesは出力しません。

Blender 5.1.1以外での動作は保証していません。詳細は[日本語マニュアル](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/blob/v1.0.0/docs/USER_MANUAL.ja.md)を確認してください。

## 検証

- Blender 5.1.1 `--background --factory-startup --disable-autoexec`
- 完全変換、KHR材質、連続Alpha、Alpha Clip、任意Shader近似、Materialフォールバック、静的instance、再import、原子的置換を含む自動テスト44件
- 219 Node / 68 Meshの実シーンで、world transform、階層、単一UV契約、material factor、cold/hot再読込、world bounds一致を確認
- Blender Extension検証
- 配布ZIP内容検証

## 配布ZIPの検証情報

- ファイルサイズ: 32,395 bytes
- SHA-256: `ECEF61E576DBE14178648657EB2F153C00890060FF88C7AE34D6C750DBCA7F09`
