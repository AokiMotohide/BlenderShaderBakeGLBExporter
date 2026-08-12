# Shader Bake GLB Exporter

Shader Bake GLB Exporterは、選択したMeshオブジェクトの接続済みPrincipled BSDFシェーダーをPBRテクスチャへベイクし、標準GLB 2.0として書き出すBlenderアドオンです。

## 対応環境

- Windows
- Blender 5.1.1
- Cycles
- 外部Pythonパッケージ不要

## インストール

1. [GitHub Releases](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/releases/latest)から`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。ZIPは展開しません。
2. Blenderの`Edit > Preferences > Extensions`を開きます。
3. 右上のメニューから`Install from Disk`を選び、ダウンロードしたZIPを指定します。
4. 「Shader Bake GLB Exporter」を有効にします。

詳しい操作、設定、対応範囲、エラー対処は[日本語マニュアル](docs/USER_MANUAL.ja.md)を参照してください。

## 使用方法

1. Object Modeで書き出すMeshオブジェクトを選択します。
2. 3D ViewのSidebarから「GLB Bake Export」を開きます。
3. 出力GLBパスとTexture Resolutionを指定します。
4. 「選択オブジェクトをGLB書き出し」を押します。

解像度は全Objectと全Material Slotへ共通で適用されます。512は短時間確認用、1024は既定値、2048は細部を保持する用途です。UV islandとBakeの余白は解像度の1/64です。

## 出力仕様

- 標準GLB 2.0
- 選択Meshだけを出力
- Base Color + Alpha: RGBA PNG、sRGB
- ORM: RGB PNG、linear、R=1、G=Roughness、B=Metallic
- Normal: tangent-space RGB PNG、linear
- Emissive: RGB PNG、sRGB
- Transmission: grayscaleをRGBへ複製したPNG、linear
- IOR: 定数
- 画像: 8bit PNG
- Animation、Skin、Morph、Camera、Light、Draco、gltfpack、Custom Propertiesなし

## 対応するシェーダー

Active Material OutputのSurfaceへ単一Principled BSDFが直接接続された材質に対応します。Principled BSDFのBase Color、Metallic、Roughness、Normal、Emission Color、Emission Strength、Alpha、Transmission Weightへ接続された、Cyclesで評価可能なノードをベイクします。

Image Texture、Noise Texture、Voronoi Texture、ColorRamp、Mapping、Math、Vector Math、Mix Color、Object座標、Generated座標、Node Groupを利用できます。同じMaterialを複数Objectが共有する場合も、ObjectとMaterial Slotごとに別の結果を生成します。

## 未対応

- Mix Shader、Add Shader、Principled BSDF以外のSurface Shader
- Toon BSDF
- Subsurface、Coat、Sheen、Anisotropic、Thin Film
- Shader Displacement、Volume
- Procedural IOR
- Layer Weight、Camera Data、Light Path、Fresnelなどの視線依存評価
- OSL Script
- Alpha Blend、Alpha Hashed
- Metallic-Roughness契約で保持できないPrincipled設定

Alpha Clipには対応します。しきい値を取得できない場合は0.5を使用します。

## 実行中の挙動

処理は同一Blenderプロセス内のModal Operatorで進みます。Object、Material、Channelの処理単位間でUIと進捗を更新します。Cycles Bake中は一時的に操作できません。キャンセルは現在のBakeが完了した後に反映され、新しいBakeは開始されません。

元Object、Mesh、Material、Node、Image、UV、Modifierは変更しません。評価済みコピーを作業用Collectionへ隔離します。Cycles用Render設定と選択状態は開始前に保存し、成功、失敗、キャンセルの全経路で復元します。一時データも全終了経路で削除します。

生成したGLBは、標準glTF 2.0のMetallic-Roughness材質とKHR拡張に対応するGLBビューアまたはレンダラーで読み込めます。

## テスト

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --background --factory-startup --disable-autoexec --python tests/run_tests.py
```

テスト生成物はWindowsの一時フォルダへ作成されます。

## ZIP生成

```powershell
powershell -ExecutionPolicy Bypass -File tools/package.ps1
```

`dist`は再生成可能なartifactのためGit管理しません。生成スクリプトはBlender 5.1.1のExtension検証とbuildを実行します。
