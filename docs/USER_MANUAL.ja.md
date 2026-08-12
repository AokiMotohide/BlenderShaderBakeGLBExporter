# Shader Bake GLB Exporter 日本語マニュアル

## 1. 概要

Shader Bake GLB Exporterは、選択したMeshのPrincipled BSDF材質をPBRテクスチャへベイクし、標準GLB 2.0へ書き出すBlender Extensionです。手続き型テクスチャやノードグループを、一般的なGLBビューアで扱える画像テクスチャへ変換できます。

元のObject、Mesh、Material、Node、Image、UV、Modifierは直接変更しません。処理用コピーと一時データは、成功、失敗、キャンセルのいずれでも削除されます。

## 2. 動作環境

- Windows
- Blender 5.1.1
- Cyclesが利用できる環境
- 外部Pythonパッケージは不要

Blender 5.1.1以外での動作は保証していません。

## 3. インストール

1. [GitHub Releases](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/releases/latest)を開きます。
2. Assetsから`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。
3. ZIPを展開せず、Blenderを起動します。
4. `Edit > Preferences > Extensions`を開きます。
5. 右上のメニューから`Install from Disk`を選びます。
6. ダウンロードしたZIPを指定します。
7. 「Shader Bake GLB Exporter」を有効にします。

インストール後、3D Viewportで`N`キーを押してSidebarを表示すると、`GLB Bake Export`タブが追加されます。

## 4. 基本的な書き出し手順

1. BlenderをObject Modeにします。
2. GLBへ含めるMeshオブジェクトを1個以上選択します。選択していないObjectは出力されません。
3. 3D Viewportで`N`キーを押し、`GLB Bake Export`タブを開きます。
4. `出力GLBパス`に、拡張子`.glb`を含む保存先を指定します。
5. `Texture Resolution`を選択します。
6. パネルに表示される選択Mesh数と検出Material数を確認します。
7. `選択オブジェクトをGLB書き出し`を押します。
8. 完了後、パネルに表示されるGLBパスを確認します。

書き出し前に、既存の同名GLBは置換されません。新しいGLBを一時ファイルへ書き出し、構造検証に成功した場合だけ保存先を置換します。

## 5. Texture Resolution

解像度は、すべての選択Objectと使用中Material Slotに共通で適用されます。

| 設定 | 用途 | 注意点 |
| --- | --- | --- |
| 512 | 短時間の確認 | 細部が失われやすい |
| 1024 | 標準用途 | 既定値 |
| 2048 | 細部を保持する出力 | ベイク時間とメモリ使用量が増える |

生成される画像は8bit PNGです。UV islandとベイクの余白は解像度の1/64です。

## 6. 対応する材質

Active Material OutputのSurfaceへ、単一のPrincipled BSDFが直接接続された材質に対応します。次の入力をGLBへ反映します。

- Base ColorとAlpha
- MetallicとRoughness
- Normal
- Emission ColorとEmission Strength
- Transmission Weight
- 定数IOR

Image Texture、Noise Texture、Voronoi Texture、ColorRamp、Mapping、Math、Vector Math、Mix Color、Object座標、Generated座標、Node Groupなど、Cyclesで評価可能な接続ノードをベイクできます。同じMaterialを複数Objectが共有していても、ObjectとMaterial Slotごとに結果を作成します。

## 7. 未対応の主な機能

- Mix Shader、Add Shader、Principled BSDF以外のSurface Shader
- Toon BSDF
- Subsurface、Coat、Sheen、Anisotropic、Thin Film
- Shader Displacement、Volume
- Procedural IOR
- Layer Weight、Camera Data、Light Path、Fresnelなどの視線依存評価
- OSL Script
- Alpha Blend、Alpha Hashed
- Animation、Skin、Morph、Camera、Light
- Draco、gltfpack、Custom Properties

Alpha Clipには対応します。しきい値を取得できない場合は0.5を使用します。

## 8. 実行中の操作とキャンセル

パネルには進捗、処理中のObject、Material、処理段階が表示されます。Cycles Bake中はBlenderを一時的に操作できません。

`キャンセル`を押すか`Esc`キーを押すと、現在実行中のBakeが完了した後に停止します。処理途中のGLBは最終保存先へ反映されません。

## 9. エラー対処

### 「選択Meshが0件です」

Object ModeでMeshオブジェクトを1個以上選択してください。Curve、Camera、Lightだけを選択しても出力対象になりません。

### 「出力先が未指定です」または「出力拡張子は.glbが必要です」

`出力GLBパス`へ、ファイル名と`.glb`拡張子を含む保存先を指定してください。

### 「FaceがないMeshはベイクできません」

頂点やEdgeだけでなく、Faceを持つMeshを使用してください。

### MaterialまたはShaderに関するエラー

対象MaterialのActive Material Outputを確認し、Surfaceへ単一のPrincipled BSDFを直接接続してください。未対応機能を外すか、事前に対応可能なノード構成へ変換してください。

### ベイクに時間がかかる

まず512で確認し、最終出力時に1024または2048へ上げてください。Object数、Material Slot数、画像解像度が増えるほど処理時間とメモリ使用量が増えます。

## 10. アンインストール

1. Blenderの`Edit > Preferences > Extensions`を開きます。
2. 「Shader Bake GLB Exporter」を検索します。
3. Extensionのメニューから`Uninstall`を実行します。

アンインストールしても、すでに書き出したGLBは削除されません。

## 11. 出力仕様

- 標準GLB 2.0
- 選択Meshだけを出力
- Base Color + Alpha: RGBA PNG、sRGB
- ORM: RGB PNG、linear、R=1、G=Roughness、B=Metallic
- Normal: tangent-space RGB PNG、linear
- Emissive: RGB PNG、sRGB
- Transmission: grayscaleをRGBへ複製したPNG、linear
- IOR: 定数

生成したGLBは、glTF 2.0 Metallic-Roughness材質と、使用されるKHR材質拡張に対応したビューアまたはレンダラーで読み込んでください。
