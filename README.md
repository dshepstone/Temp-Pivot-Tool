# Temp Pivot Tool for Maya

Create temporary pivots for selected objects so you can rotate them from wherever you like. It doesn't change the actual object pivot or create any permanent node in your scene - everything happens on the fly.

![Temp Pivot Tool](temp-pivot.png)

## Features

- **Temporary Pivot Creation** - Create a temporary pivot for any selected control or object
- **Interactive Pivot Locator** - Visual locator you can manipulate to position the pivot
- **Auto-Keying** - Automatically keys object transforms when manipulating with temp pivot
- **Stored Pivots** - Save pivot configurations to scene nodes for later recall
- **Toggle On/Off** - Quickly switch between temp pivot and original pivot
- **Smart Euler Filter** - Prevent rotation flipping issues
- **Modern UI** - Clean, organized interface with visual state indicators

## Installation

### Drag and Drop (Recommended)

1. Download or clone this repository
2. Drag the `install_temp_pivot_tool.mel` file into the Maya viewport
3. The tool will be installed and a shelf button will be created

### Manual Installation

1. Copy `temp_pivot_tool.py` to your Maya scripts folder:
   - **Windows:** `C:\Users\<username>\Documents\maya\<version>\scripts\`
   - **macOS:** `~/Library/Preferences/Autodesk/maya/<version>/scripts/`
   - **Linux:** `~/maya/<version>/scripts/`

2. Copy `temp-pivot.png` to your Maya icons folder:
   - **Windows:** `C:\Users\<username>\Documents\maya\<version>\prefs\icons\`
   - **macOS:** `~/Library/Preferences/Autodesk/maya/<version>/prefs/icons/`
   - **Linux:** `~/maya/<version>/prefs/icons/`

3. Launch the tool:
   ```python
   import temp_pivot_tool
   temp_pivot_tool.show()
   ```

## Usage

### Basic Workflow

1. **Select** one or more objects/controls
2. **Choose** a pivot mode:
   - *Pivot to Last Selected* - Use the last selected object's pivot
   - *Pivot to Selection Center* - Center of all selected objects
   - *Pivot to World Origin* - Use world origin (0,0,0)
   - *Pivot to Custom Locator* - Use a specified locator
   - *Pivot to Component Center* - Center of selected components
3. **Apply** the temp pivot
4. **Rotate/Scale** your objects using the temp pivot
5. **Reset** to restore original pivots when done

### Interactive Pivot Locator

1. Click **Create Locator** to create a visual pivot control
2. Move the locator to your desired pivot position
3. Click **Update from Locator** to apply the new pivot position
4. The locator can be deleted when no longer needed

### Storing Pivots

Store frequently used pivot configurations for quick recall:

1. Set up your desired pivot configuration
2. Enter a name in the "Pivot Name" field
3. Click **Store** to save the configuration
4. Later, select the stored pivot and click **Recall** to restore it

### Auto-Keying

When enabled, the tool automatically sets keyframes on object transforms when you manipulate objects using the temp pivot:

1. Enable Maya's Auto Key feature
2. Enable "Tool Auto-Key" in the Auto-Key Settings section
3. Manipulate your objects - keyframes are set automatically

### Smart Euler Filter

If you notice rotation weirdness between two keys after using this tool, enable the **Smart Euler Filter** option. This applies Euler filtering to rotation curves to prevent gimbal flipping.

## Pivot Modes

| Mode | Description |
|------|-------------|
| **Pivot to Last Selected** | Uses the rotate pivot of the last selected object |
| **Pivot to Selection Center** | Calculates the center point of all selected objects |
| **Pivot to World Origin** | Sets pivot to (0, 0, 0) in world space |
| **Pivot to Custom Locator** | Uses the position of a specified locator |
| **Pivot to Component Center** | Centers on selected vertices, edges, or faces |

## Data Storage

All tool data is stored in Maya network nodes within your scene:

- **tempPivotManager** - Global tool settings and stored configurations
- **tempPivotData_[objectName]** - Per-object pivot state

This means your pivot configurations are saved with your scene and available when you reopen it.

## API Reference

The tool can be used programmatically:

```python
import temp_pivot_tool

# Show the UI
temp_pivot_tool.show()

# Apply temp pivot programmatically
controls = ["pCube1", "pSphere1"]
world_point = (0, 5, 0)
temp_pivot_tool.apply_temp_pivot(controls, world_point, affect_scale=True, mode="Custom")

# Restore original pivots
temp_pivot_tool.restore_temp_pivot(controls)

# Store a pivot configuration
temp_pivot_tool.store_pivot("my_pivot", controls, world_point, "Custom", True)

# Recall a stored pivot
temp_pivot_tool.recall_pivot("my_pivot")
```

## Keyboard Shortcuts

You can create a hotkey for quick access:

```python
import temp_pivot_tool; temp_pivot_tool.show()
```

## Uninstallation

To uninstall the tool, run in Maya's Script Editor:

```mel
tempPivot_Uninstall();
```

Or manually delete:
- The `temp_pivot_tool.py` from your scripts folder
- The `temp-pivot.png` from your icons folder
- The "Temp Pivot" shelf button

## Requirements

- Autodesk Maya 2018 or later
- Python 2.7+ (Maya 2018-2021) or Python 3.7+ (Maya 2022+)

## Version History

### v2.0.0
- Added interactive pivot locator
- Added auto-keying system
- Added stored pivot management
- Added Smart Euler Filter support
- Added component center pivot mode
- Modernized UI design
- Added visual ON/OFF state indicator
- Added output logging panel
- Improved installer with uninstall support

### v1.0.0
- Initial release
- Basic temp pivot functionality
- Preset system

## License

MIT License - See [LICENSE](LICENSE) for details.

## Author

David Shepstone

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
