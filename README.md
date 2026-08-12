# Shader Bake GLB Exporter

Shader Bake GLB Exporterは、選択したMeshの接続済みシェーダーをPBRテクスチャへベイクし、標準GLB 2.0として書き出すBlender Extensionです。Principled BSDFはglTF材質へ変換し、それ以外のSurface Shaderは外観をCore PBRへ近似して、可能な限り書き出しを継続します。

## 対応環境

- Windows
- Blender 5.1.1
- Cycles
- 外部Pythonパッケージ不要

## インストール

1. [GitHub Releases](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/releases/latest)から`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。ZIPは展開しません。
2. Blenderの`Edit > Preferences > Extensions`を開きます。
3. 右上のメニューから`Install from Disk`を選び、ZIPを指定します。
4. 「Shader Bake GLB Exporter」を有効にします。

詳しい操作方法は[日本語マニュアル](docs/USER_MANUAL.ja.md)を参照してください。

## 使用方法

1. Object Modeで書き出すMeshを選択します。
2. 3D ViewのSidebarから「GLB Bake Export」を開きます。
3. Texture Resolutionを選択し、「GLBを書き出し…」を押します。
4. 標準保存ダイアログで保存先を確認し、「GLBを書き出し」を実行します。

保存ダイアログには`.glb`が自動で付きます。初回名は、保存済みBlendではBlend名、未保存時はActive Mesh名、どちらも利用できない場合は`export.glb`です。2回目以降は前回の保存先を再利用します。

## 出力仕様

- 標準GLB 2.0
- 選択Meshだけを出力
- 選択Mesh間の親子関係と、配置に必要なEmpty祖先をglTF Node階層として保持
- Base Color + Alpha: RGBA PNG、sRGB
- ORM: RGB PNG、Linear、R=Occlusion、G=Roughness、B=Metallic
- Normal: tangent-space RGB PNG、Linear
- Emissive: RGB PNG、sRGB
- 全texture slotを単一のBake UVへ統一し、`TEXCOORD_0`で出力
- 定数Base Color、Alpha、Metallic、Roughness、Emissive、IORを標準glTF factorとして保持
- 画像: 8bit PNG
- 対応する場合はTransmission、IOR、Specular、Clearcoat、Sheen、Anisotropy、Volume、Emissive Strength、Unlit用KHR拡張を出力

## 材質の変換

Active Material OutputへPrincipled BSDFが直接接続され、glTFで表現できる材質は、各入力を個別にベイクしてPBRおよびKHR材質へ変換します。Image Texture、Noise Texture、Voronoi Texture、ColorRamp、Mapping、Math、Vector Math、Mix Color、Object座標、Generated座標、Node Groupなど、Cyclesで評価できる接続を利用できます。

glTFに直接対応しないSurface Shaderや機能は、Diffuse、Glossy、Transmission、Roughness、Normal、Emission、AlphaのBake結果からCore PBR材質へ近似します。対象にはMix Shader、Add Shader、Glass、Toon、Subsurface、Thin Film、Shader Displacement、視線依存ノードなどが含まれます。

個別チャンネルのBakeに失敗した場合も、次の安全値へ置換して処理を継続します。

- Base Color: 元Materialのviewport色、取得できない場合は0.8灰色
- Metallic: 0
- Roughness: 0.5
- Normal: フラット
- Emission: 0
- Transmission: 0
- IOR: 1.5

Material未割当、Node未使用、Surface未接続も既定PBR材質へ置換します。近似または置換した内容はSidebarの「警告一覧」へ表示されます。

## Alpha

- 不透明: `alphaMode: OPAQUE`
- Alpha Clip: `alphaMode: MASK`と`alphaCutoff`
- 連続Alpha、Alpha Blend、Alpha Hashed相当: `alphaMode: BLEND`

glTFにはAlpha Hashedと同一のモードがないため、連続AlphaとしてBLENDへ変換します。

## 制約

- 近似材質は元Shaderとの物理的・視線依存な完全一致を保証しません。
- VolumeとShader DisplacementはGLBで保持できる範囲だけを出力し、それ以外は省略して警告します。
- Animation、Skin、Morph、Camera、Light、Draco、gltfpack、Custom Propertiesは出力しません。
- 解像度は全Objectと全Material Slotへ共通で適用します。512、1024、2048から選択します。

元Object、Mesh、Material、Node、Image、UV、Modifier、Scene設定、Render設定は直接変更しません。一時データは成功、失敗、キャンセルの全経路で削除します。GLBは一時ファイルへ出力し、構造検証に成功した場合だけ最終パスへ原子的に置換します。

出力検証ではNodeのTRS/matrix排他、階層参照、非循環性、Mesh/Material参照、全texture slotの実効UV set/transform一致、主要factorの値域を確認します。Bake後は全slotが同じ`TEXCOORD_0`を使うため、元材質に異なるUV setやMappingがあっても`KHR_texture_transform`へ依存しません。

## テスト

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --background --factory-startup --disable-autoexec --python tests/run_tests.py
```

テスト生成物はWindowsの一時フォルダへ作成されます。

## ZIP生成

```powershell
powershell -ExecutionPolicy Bypass -File tools/package.ps1
```

`dist`は再生成可能なartifactのためGit管理しません。生成スクリプトはBlender 5.1.1のExtension検証、build、ZIP内容検証を実行します。
