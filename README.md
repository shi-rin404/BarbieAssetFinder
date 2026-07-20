# Asset Finder for Certain Game Archive Format
The asset lookup algorithm in certain Doll Dressing-up game has implemented.

It inputs one or multiple asset pathes and decompresses it from game archive.

# Credits
Archive extractor by [MarcosVLl2](https://github.com/MarcosVLl2) from [NeoXtractor](https://github.com/MarcosVLl2/NeoXtractor)

Install dependencies:

```powershell
pip install -r requirements.txt
```

## GUI
Execute **"Z_GUI.bat"** or;

```powershell
python gui.py
```

<img width="1536" height="816" alt="image" src="https://github.com/user-attachments/assets/0cc98f38-a4d3-4bfb-8e6a-471e28edcd3d" />

## Interactive CLI
Execute **"Z_CLI.bat"** or;

```powershell
python cli.py
```

Press `Enter` to add the typed path into the queue and press `Tab` to search and decompress the queued paths.

## CLI with arguments
- Split multiple inputs by space
- You might not use " since game pathes naturally doesn't include spaces

```powershell
python cli.py "chr/player/dm65_survivor_w/h55_survivor_w_jyz/separate_dir/jyz_e_xiari/jyz_e_xiari.gim" "chr/prop/h55_pendant_quanzhang/separate_dir/jyz_e_xiari/jyz_e_xiari_quanzhang.gim"
```
