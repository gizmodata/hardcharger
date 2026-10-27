param([Parameter(Mandatory = $true)][string]$Installer)
$ErrorActionPreference = 'Stop'

# Verify the shipped MSI's Binary table, not just the source configuration.
# ReadStream format 1 returns one byte per BSTR character:
# https://learn.microsoft.com/en-us/windows/win32/msi/record-readstream
$api = New-Object -ComObject WindowsInstaller.Installer
$database = $api.OpenDatabase((Resolve-Path $Installer).Path, 0)
try {
    foreach ($entry in @(
        @{ Id = 'WixUI_Bmp_Banner'; File = 'banner.bmp' },
        @{ Id = 'WixUI_Bmp_Dialog'; File = 'dialog.bmp' }
    )) {
        $expected = Join-Path $PSScriptRoot "../src-tauri/installer/$($entry.File)"
        $query = 'SELECT `Data` FROM `Binary` WHERE `Name` = ''{0}''' -f $entry.Id
        $view = $database.OpenView($query)
        $record = $null
        try {
            $view.Execute()
            $record = $view.Fetch()
            if ($null -eq $record) { throw "MSI is missing $($entry.Id)" }
            [string]$data = $record.ReadStream(1, 1048576, 1)
            $bytes = [Text.Encoding]::Latin1.GetBytes($data)
            $actual = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
            if ($actual -ne (Get-FileHash $expected -Algorithm SHA256).Hash) {
                throw "MSI has incorrect artwork for $($entry.Id)"
            }
            Write-Host "$($entry.Id): embedded Hardcharger artwork verified"
        } finally {
            if ($null -ne $record) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record) }
            $view.Close()
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
        }
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($database)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($api)
}
