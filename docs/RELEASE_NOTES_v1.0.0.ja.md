# Shader Bake GLB Exporter v1.0.0

選択したMeshの接続済みPrincipled BSDFシェーダーをPBRテクスチャへベイクし、標準GLB 2.0として書き出すBlender Extensionの初回リリースです。

## ダウンロードとインストール

1. このReleaseのAssetsから`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。
2. ZIPを展開せず、Blender 5.1.1の`Edit > Preferences > Extensions`を開きます。
3. 右上のメニューから`Install from Disk`を選び、ZIPを指定します。
4. 「Shader Bake GLB Exporter」を有効にします。

詳しい操作方法は[日本語マニュアル](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/blob/v1.0.0/docs/USER_MANUAL.ja.md)を参照してください。

## 主な機能

- 選択したMeshだけをGLBへ出力
- Base Color + Alpha、ORM、Normal、Emissive、Transmissionを8bit PNGへベイク
- IOR、Alpha Clip、Emission Strength用KHR拡張を出力
- Object座標、Generated座標、手続き型テクスチャ、Node Groupに対応
- 元データを変更せず、成功・失敗・キャンセル時に一時データを削除
- 検証済みGLBだけを最終保存先へ反映

## 対応環境

- Windows
- Blender 5.1.1
- Cycles

Blender 5.1.1以外での動作は保証していません。未対応機能と制約は[日本語マニュアル](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/blob/v1.0.0/docs/USER_MANUAL.ja.md#7-未対応の主な機能)を確認してください。
