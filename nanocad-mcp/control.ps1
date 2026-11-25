param([switch]$InstallShortcut)

$ErrorActionPreference = 'Stop'

if ($InstallShortcut) {
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop 'nanoCAD MCP.lnk'))
    $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Параметры подключения nanoCAD к ChatGPT через MCP'
    $shortcut.IconLocation = (Join-Path $PSScriptRoot 'NanoCAD-MCP.ico') + ',0'
    $shortcut.Save()
    $threeD = $shell.CreateShortcut((Join-Path $desktop 'nanoCAD MCP 3D.lnk'))
    $threeD.TargetPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    $threeD.Arguments = '"{0}" --toggle-pack 3d --notify' -f (Join-Path $PSScriptRoot 'tool_catalog.py')
    $threeD.WorkingDirectory = $PSScriptRoot
    $threeD.IconLocation = (Join-Path $PSScriptRoot 'NanoCAD-MCP-3D.ico') + ',0'
    $threeD.Description = 'Включить или выключить набор 3D-команд nanoCAD MCP'
    $threeD.Save()
    return
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

try {
    $form = [Windows.Forms.Form]::new()
    $form.Text = 'nanoCAD MCP'
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.AutoScaleMode = 'Dpi'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.ClientSize = [Drawing.Size]::new(650, 615)

    $panel = [Windows.Forms.TableLayoutPanel]::new()
    $panel.Dock = 'Fill'
    $panel.Padding = [Windows.Forms.Padding]::new(18, 14, 18, 14)
    $panel.ColumnCount = 1
    [void]$panel.ColumnStyles.Add([Windows.Forms.ColumnStyle]::new('Percent', 100))
    $form.Controls.Add($panel)

    function Add-Label([string]$Text, [int]$TopMargin = 0) {
        $label = [Windows.Forms.Label]::new()
        $label.Text = $Text
        $label.AutoSize = $true
        $label.Margin = [Windows.Forms.Padding]::new(3, $TopMargin, 3, 2)
        $panel.Controls.Add($label)
    }

    function Add-PathField([string]$Label, [string]$Path) {
        Add-Label $Label 6
        $field = [Windows.Forms.TextBox]::new()
        $field.Text = $Path
        $field.ReadOnly = $true
        $field.Dock = 'Fill'
        $field.add_Enter({ $this.SelectAll() })
        $panel.Controls.Add($field)
    }

    Add-Label 'Подключение к ChatGPT для Windows · тип STDIO'
    Add-PathField 'Команда' (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
    Add-PathField 'Аргумент' (Join-Path $PSScriptRoot 'nanocad_mcp.py')

    Add-Label 'Включите доступ внутри nanoCAD на вкладке MCP или панели «nanoCAD MCP».' 12
    Add-Label 'Доступные команды · галочка у группы переключает все её команды' 8
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $catalog = & $python (Join-Path $PSScriptRoot 'nanocad_mcp.py') --tool-catalog
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось прочитать каталог команд.' }
    $tree = [Windows.Forms.TreeView]::new()
    $tree.CheckBoxes = $true
    $tree.Height = 315
    $tree.Dock = 'Top'
    $groups = @{}
    foreach ($entry in ($catalog | ConvertFrom-Json)) {
        if (!$groups.ContainsKey($entry.group)) {
            $groups[$entry.group] = $tree.Nodes.Add([string]$entry.group)
        }
        $child = $groups[$entry.group].Nodes.Add([string]$entry.name)
        $child.Checked = [bool]$entry.enabled
        $child.Tag = $child.Checked
    }
    function Update-Group($node) {
        $count = @($node.Nodes | Where-Object Checked).Count
        $name = [string]$node.Tag
        $node.Text = "$name ($count/$($node.Nodes.Count))"
        $node.Checked = $count -eq $node.Nodes.Count
    }
    foreach ($node in $tree.Nodes) { $node.Tag = $node.Text; Update-Group $node }
    $tree.add_AfterCheck({
        param($sender,$eventArgs)
        if ($eventArgs.Action -eq [Windows.Forms.TreeViewAction]::Unknown) { return }
        $node = $eventArgs.Node
        if ($null -eq $node.Parent) {
            foreach ($child in $node.Nodes) { $child.Checked = $node.Checked }
            Update-Group $node
        } else { Update-Group $node.Parent }
    })
    $panel.Controls.Add($tree)
    $apply = [Windows.Forms.Button]::new()
    $apply.Text = 'Применить'
    $apply.AutoSize = $true
    $apply.add_Click({
        $delta = @{enable=@(); disable=@()}
        foreach ($group in $tree.Nodes) {
            foreach ($node in $group.Nodes) {
                if ($node.Checked -ne $node.Tag) {
                    if ($node.Checked) { $delta.enable += $node.Text }
                    else { $delta.disable += $node.Text }
                }
            }
        }
        try {
            $delta | ConvertTo-Json -Compress | & $python (Join-Path $PSScriptRoot 'tool_catalog.py') --apply-selection | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось сохранить настройки команд.' }
            $form.Close()
        } catch { [void][Windows.Forms.MessageBox]::Show($_.Exception.Message) }
    })
    $panel.Controls.Add($apply)
    [void]$form.ShowDialog()
} finally {
    if ($form) { $form.Dispose() }
}
