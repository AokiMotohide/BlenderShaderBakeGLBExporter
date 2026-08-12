# Shader Bake GLB Exporter 日本語マニュアル

## 1. 概要

Shader Bake GLB Exporterは、選択したMeshの接続済みシェーダーをPBRテクスチャへベイクし、標準GLB 2.0へ書き出すBlender Extensionです。

Principled BSDFのglTF対応入力はPBRおよびKHR材質へ変換します。直接変換できないShaderも外観をCore PBRへ近似し、個別Bakeに失敗したチャンネルは安全値へ置換して、可能な限りGLB生成を継続します。

元Object、Mesh、Material、Node、Image、UV、Modifierは変更しません。一時データは成功、失敗、キャンセルの全経路で削除されます。

## 2. 動作環境

- Windows
- Blender 5.1.1
- Cyclesが利用できる環境
- 外部Pythonパッケージ不要

Blender 5.1.1以外での動作は保証していません。

## 3. インストール

1. [GitHub Releases](https://github.com/AokiMotohide/BlenderShaderBakeGLBExporter/releases/latest)を開きます。
2. Assetsから`shader_bake_glb_exporter-1.0.0.zip`をダウンロードします。
3. ZIPを展開せず、Blenderを起動します。
4. `Edit > Preferences > Extensions`を開きます。
5. 右上のメニューから`Install from Disk`を選びます。
6. ダウンロードしたZIPを指定します。
7. 「Shader Bake GLB Exporter」を有効にします。

3D Viewportで`N`キーを押してSidebarを表示すると、「GLB Bake Export」タブが追加されます。

## 4. 書き出し手順

1. BlenderをObject Modeにします。
2. GLBへ含めるMeshを1件以上選択します。
3. Sidebarの「GLB Bake Export」を開きます。
4. `Texture Resolution`を選択します。
5. 選択Mesh数と検出Material数を確認します。
6. 「GLBを書き出し…」を押します。
7. 標準保存ダイアログで保存先を確認し、「GLBを書き出し」を実行します。

保存名に拡張子を入力しなくても`.glb`が自動で付きます。初回のファイル名は次の順で決まります。

1. 保存済みBlendのファイル名
2. 未保存時のActive Mesh名
3. `export.glb`

2回目以降は前回の保存先を再利用します。同名GLBがある場合はBlender標準の上書き確認が表示されます。

## 5. Texture Resolution

| 設定 | 用途 | 注意点 |
| --- | --- | --- |
| 512 | 短時間の確認 | 細部が失われやすい |
| 1024 | 標準用途 | 既定値 |
| 2048 | 細部を保持する出力 | ベイク時間とメモリ使用量が増える |

解像度は、すべての選択Objectと使用中Material Slotへ共通で適用されます。生成画像は8bit PNGです。

## 6. 完全変換する材質

Active Material OutputのSurfaceへPrincipled BSDFが直接接続されている場合、次の入力をベイクします。

- Base ColorとAlpha
- Occlusion、Metallic、Roughness
- Normal
- Emission ColorとEmission Strength
- Transmission WeightとIOR
- Specular IOR LevelとSpecular Tint
- Coat Weight、Coat Roughness、Coat Normal
- Sheen Weight、Sheen Tint、Sheen Roughness
- AnisotropicとAnisotropic Rotation
- glTF Material OutputのThicknessとPrincipled Volume

必要に応じて`KHR_materials_transmission`、`KHR_materials_ior`、`KHR_materials_specular`、`KHR_materials_clearcoat`、`KHR_materials_sheen`、`KHR_materials_anisotropy`、`KHR_materials_volume`、`KHR_materials_emissive_strength`を出力します。Emissionだけの材質は`KHR_materials_unlit`として出力します。

Principled入力へ接続された、Cyclesで評価可能な手続き型テクスチャやNode Groupもベイクできます。

## 7. 外観近似とフォールバック

Principled BSDFへ完全変換できない材質は、Diffuse、Glossy、Transmission、Roughness、Normal、Emission、AlphaのBake結果からCore PBRへ近似します。

主な対象は次のとおりです。

- Mix Shader、Add Shader、Principled以外のBSDF
- Toon、Glass、Subsurface、Thin Film
- Procedural IOR
- Shader Displacement
- Camera Data、Light Path、Layer Weight、Fresnelなどの視線依存評価
- glTFで直接保持できないVolume表現

近似材質は、元Shaderとの物理的・視線依存な完全一致を保証しません。省略または近似した内容は「警告一覧」に表示されます。

個別チャンネルのBakeが失敗した場合は、次の値へ置換してJobを継続します。

| チャンネル | 既定値 |
| --- | --- |
| Base Color | viewport色、取得不能時は0.8灰色 |
| Metallic | 0 |
| Roughness | 0.5 |
| Normal | フラット |
| Emission | 0 |
| Transmission | 0 |
| IOR | 1.5 |

Material未割当、Node未使用、Surface未接続も既定PBR材質へ置換します。FaceなしMeshは警告付きで除外されます。書き出せるMeshが1件も残らない場合は失敗します。

## 8. Alpha

| Blender材質 | GLB |
| --- | --- |
| 不透明 | `alphaMode: OPAQUE` |
| Alpha Clip | `alphaMode: MASK`と`alphaCutoff` |
| 連続Alpha、Alpha Blend | `alphaMode: BLEND` |
| Alpha Hashed相当 | `alphaMode: BLEND` |

glTF 2.0にはAlpha Hashedと同一のモードがないため、連続AlphaのBLENDへ変換します。

## 9. 実行中の表示とキャンセル

Sidebarには進捗、処理中Object、Material、チャンネルが表示されます。Cycles Bake中はBlenderを一時的に操作できません。

「キャンセル」または`Esc`を押すと、現在実行中のBakeが完了した後に停止します。未検証の一時GLBは最終保存先へ反映されません。

成功時に近似や置換が発生した場合は「警告一覧」が表示されます。GLB生成を中止した問題だけが「エラー一覧」に表示されます。

## 10. Jobが失敗する条件

- 選択Meshが0件、または書き出せるFaceを持つMeshが0件
- 出力先が未指定
- Texture Resolutionが契約外
- ファイル作成または原子的置換に失敗
- Blender標準glTF exporterがGLBを生成しない
- 生成GLBのヘッダー、Mesh、Material、Texture、Alpha、KHR拡張などの構造検証に失敗

材質表現の非対応だけでは原則として失敗せず、警告付きフォールバックになります。

## 11. 出力対象外

- Animation
- Skin
- Morph
- Camera
- Light
- Draco
- gltfpack
- Custom Properties

## 12. 安全性

元データは直接変更しません。Modifier適用済みMesh、Material、Node、Image、UV、Node GroupなどはJob専用コピーとして作成されます。

生成GLBは最終保存先と同じフォルダの一時ファイルへ出力します。GLB 2.0構造、必要Attribute、Material、PNG、Alpha、KHR拡張を検証し、成功した場合だけ最終パスへ原子的に置換します。失敗時は既存の正常なGLBを維持します。

## 13. アンインストール

1. Blenderの`Edit > Preferences > Extensions`を開きます。
2. 「Shader Bake GLB Exporter」を検索します。
3. Extensionのメニューから`Uninstall`を実行します。

アンインストールしても、すでに書き出したGLBは削除されません。
