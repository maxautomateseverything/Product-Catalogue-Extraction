# Catalogue Table Extractor - beginner launcher
# Run with:
#   powershell -ExecutionPolicy Bypass -File ".\run_extractor.ps1"

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Write-Heading([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor Cyan
}

function Pause-ForUser {
    Write-Host ""
    Read-Host "Press Enter to continue"
}

function Select-File {
    param(
        [string]$Title,
        [string]$Filter,
        [bool]$Optional = $false
    )
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.FileName
    }
    if ($Optional) { return $null }
    throw "A required file was not selected."
}

function Select-Folder {
    param([string]$Description)
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = $true
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    throw "An output folder was not selected."
}

function Ensure-OutputFolder {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "The output-folder path is empty."
    }

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            New-Item -ItemType Directory -Path $Path -Force | Out-Null
        }

        # Confirm that Windows can write into the selected folder before the
        # extractor starts.
        $testFile = Join-Path $Path ".catalogue_extractor_write_test.tmp"
        Set-Content -LiteralPath $testFile -Value "write-test" -Encoding UTF8
        Remove-Item -LiteralPath $testFile -Force
    }
    catch {
        throw (
            "The output folder could not be created or written to: '$Path'. " +
            "Choose an existing local folder, or create the folder manually. " +
            "Windows reported: " + $_.Exception.Message
        )
    }
}

function Read-Default {
    param([string]$Prompt, [string]$Default)
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Read-YesNo {
    param([string]$Prompt, [bool]$Default = $true)
    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $answer = (Read-Host "$Prompt $suffix").Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer -in @("y", "yes")
}

function Get-PythonCommand {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        if ($py.Source) { return $py.Source }
        return $py.Name
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        if ($python.Source) { return $python.Source }
        return $python.Name
    }

    throw "Python was not found. Install Python 3.11 or 3.12 and select 'Add Python to PATH'."
}

function Test-OllamaModel([string]$Model) {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        throw "Ollama was not found. Install Ollama or choose a non-AI run mode."
    }
    $models = & ollama list 2>$null | Out-String
    if ($models -notmatch [regex]::Escape($Model)) {
        Write-Host ""
        Write-Host "The model '$Model' is not listed by Ollama." -ForegroundColor Yellow
        Write-Host "Download it with:" -ForegroundColor Yellow
        Write-Host "  ollama pull $Model" -ForegroundColor White
        if (-not (Read-YesNo "Continue anyway?" $false)) {
            throw "Run cancelled because the selected Ollama model is unavailable."
        }
    }
}

function Save-JsonConfig([hashtable]$Config, [string]$Path) {
    $clean = [ordered]@{}
    foreach ($entry in $Config.GetEnumerator()) {
        if ($null -ne $entry.Value -and "$($entry.Value)" -ne "") {
            $clean[$entry.Key] = $entry.Value
        }
    }
    $clean | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-Extractor([string]$Python, [string]$Script, [string]$ConfigFile) {
    Write-Host ""
    Write-Host "Starting extractor..." -ForegroundColor Green
    Write-Host "Python executable: $Python" -ForegroundColor DarkGray
    Write-Host "`"$Python`" `"$Script`" --config `"$ConfigFile`"" -ForegroundColor DarkGray

    & $Python $Script --config $ConfigFile

    if ($LASTEXITCODE -ne 0) {
        throw "The extractor returned exit code $LASTEXITCODE."
    }
}

try {
    Write-Heading "Catalogue Table Extractor Version 9 - Guided Setup"

    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $script = Join-Path $scriptDir "catalogue_table_extractor.py"
    $dynamicModule = Join-Path $scriptDir "dynamic_table_model.py"
    if (-not (Test-Path -LiteralPath $script)) {
        throw "catalogue_table_extractor.py was not found beside this launcher."
    }
    if (-not (Test-Path -LiteralPath $dynamicModule)) {
        throw "dynamic_table_model.py was not found beside catalogue_table_extractor.py."
    }

    $python = Get-PythonCommand

    Write-Host "Choose a run mode:"
    Write-Host "  1. Small deterministic test"
    Write-Host "  2. Full deterministic catalogue run (recommended first production pass)"
    Write-Host "  3. Registry-targeted deterministic run"
    Write-Host "  4. AI exception pass from an existing review_queue.csv"
    Write-Host "  5. Full AI automatic structuring run"
    Write-Host "  6. Resume/rebuild a previous full run"
    $mode = Read-Default "Run mode" "1"

    Write-Host ""
    Write-Host "Version 9 should use a new output folder for its first run." -ForegroundColor Yellow
    Write-Host "Older checkpoints do not contain the new SKU-anchor graph and relationship model." -ForegroundColor Yellow
    $pdf = Select-File "Select the full catalogue PDF" "PDF files (*.pdf)|*.pdf"
    $skuRegistry = Select-File "Select the canonical SKU registry (Cancel if unavailable)" "CSV files (*.csv)|*.csv|All files (*.*)|*.*" $true
    $indexRows = Select-File "Select the SKU index rows file (Cancel if unavailable)" "CSV files (*.csv)|*.csv|All files (*.*)|*.*" $true
    $tableProfile = Select-File "Select an optional reusable table profile (Cancel to use generic logic)" "JSON files (*.json)|*.json|All files (*.*)|*.*" $true
    $output = Select-Folder "Select or create a NEW Version 9 output folder"
    Ensure-OutputFolder $output

    $manufacturer = Read-Default "Manufacturer" ([IO.Path]::GetFileNameWithoutExtension($pdf))
    $catalogueId = Read-Default "Catalogue ID" "$manufacturer-catalogue"

    $pages = "all"
    if ($mode -eq "1") {
        $pages = Read-Default "PDF pages for the test, for example 33,65,80 or 1-10" "1-5"
    } elseif ($mode -eq "3") {
        $pages = "registry"
    }

    $config = @{
        pdf = $pdf
        catalogue_id = $catalogueId
        manufacturer = $manufacturer
        pages = $pages
        output = $output
        sku_registry = $skuRegistry
        sku_index_rows = $indexRows
        table_profile = $tableProfile
        catalogue_page_offset = "auto"
        index_page_radius = 1
        code_registry_scope = "all"
        layout_batch_size = 10
        layout_process_mode = "inline"
        layout_timeout = 1800
        use_ocr = $false
        resume = ($mode -eq "6")
        ai_mode = "validate"
        ai_review_policy = "auto"
        ai_structure_input = "auto"
        ollama_num_ctx = 16384
        ollama_timeout = 1800
        ollama_keep_alive = "60m"
        ai_low_confidence_action = "keep-deterministic"
        continuation_auto_threshold = 0.90
        continuation_review_threshold = 0.70
        log_level = "INFO"
    }

    if ($mode -eq "4" -or $mode -eq "5") {
        $model = Read-Default "Ollama model" "qwen3-vl:4b-instruct"
        Test-OllamaModel $model
        $config["ollama_model"] = $model
        $config["ai_mode"] = "structure"

        if ($mode -eq "4") {
            $queue = Select-File "Select the existing review_queue.csv" "CSV files (*.csv)|*.csv"
            $config["review_queue_input"] = $queue
            $config["resume"] = $false
        }
    }

    Ensure-OutputFolder $output
    $configFile = Join-Path $output "last_run_config.json"
    Save-JsonConfig $config $configFile

    Write-Heading "Configuration summary"
    Write-Host "PDF:           $pdf"
    Write-Host "SKU registry:  $skuRegistry"
    Write-Host "Index rows:    $indexRows"
    Write-Host "Table profile: $tableProfile"
    Write-Host "Pages:         $($config['pages'])"
    Write-Host "Output:        $output"
    Write-Host "Config:        $configFile"
    if ($config.ContainsKey("ollama_model")) {
        Write-Host "Ollama model:  $($config['ollama_model'])"
    }

    if (-not (Read-YesNo "Start now?" $true)) {
        Write-Host "Configuration saved. Run later with:"
        Write-Host "  $python `"$script`" --config `"$configFile`""
        exit 0
    }

    Invoke-Extractor $python $script $configFile

    if ($mode -eq "4") {
        Write-Heading "AI exception pass complete"
        Write-Host "The selected pages have been reprocessed."
        Write-Host "The top-level combined CSV files currently reflect that exception pass."
        if (Read-YesNo "Rebuild the complete catalogue aggregates now?" $true) {
            $rebuild = @{}
            foreach ($key in $config.Keys) { $rebuild[$key] = $config[$key] }
            $rebuild.Remove("review_queue_input")
            $rebuild.Remove("ollama_model")
            $rebuild["pages"] = "all"
            $rebuild["resume"] = $true
            $rebuild["ai_mode"] = "validate"
            $rebuildConfig = Join-Path $output "rebuild_config.json"
            Save-JsonConfig $rebuild $rebuildConfig
            Invoke-Extractor $python $script $rebuildConfig
        }
    }

    Write-Heading "Finished"
    Write-Host "Open this folder in File Explorer:"
    Write-Host "  $output" -ForegroundColor Green
    Write-Host ""
    Write-Host "Review these files first:"
    Write-Host "  manifest.json"
    Write-Host "  products.csv"
    Write-Host "  product_attributes.csv"
    Write-Host "  product_occurrences.csv"
    Write-Host "  review_queue.csv"
    Write-Host "  unmatched_registry_skus.csv"
    Write-Host "  unexpected_pdf_skus.csv"
    Pause-ForUser
}
catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "No source files were changed. Check README.md for troubleshooting."
    Pause-ForUser
    exit 1
}
