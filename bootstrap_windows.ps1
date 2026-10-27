# Native Windows GUI is available before Python or PyQt is installed.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$form = New-Object Windows.Forms.Form
$form.Text = 'TEMPO Setup'
$form.Size = New-Object Drawing.Size(560,180)
$form.StartPosition = 'CenterScreen'
$label = New-Object Windows.Forms.Label
$label.Text = 'Preparing TEMPO. Downloading Python and graphical setup dependencies…'
$label.Location = New-Object Drawing.Point(20,20)
$label.Size = New-Object Drawing.Size(510,50)
$bar = New-Object Windows.Forms.ProgressBar
$bar.Location = New-Object Drawing.Point(20,80)
$bar.Size = New-Object Drawing.Size(510,20)
$bar.Style = 'Marquee'
$form.Controls.AddRange(@($label,$bar))
$job = Start-Job -ArgumentList $root -ScriptBlock {
    param($root)
    $ErrorActionPreference = 'Stop'
    Set-Location $root
    function Run-Hidden($file, $arguments) {
        $process = Start-Process -FilePath $file -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "$file failed (exit $($process.ExitCode))." }
    }
    $python = Join-Path $root '.runtime\python.exe'
    $venv = Join-Path $root '.venv\Scripts\python.exe'
    if (!(Test-Path $venv)) {
        if (!(Test-Path $python)) {
            Write-Output 'Downloading Python 3.13…'
            $installer = Join-Path $env:TEMP 'tempo-python-3.13.16.exe'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe' -OutFile $installer -UseBasicParsing
            $signature = Get-AuthenticodeSignature $installer
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Python installer signature verification failed.' }
            Write-Output 'Installing private Python runtime…'
            $runtime = Join-Path $root '.runtime'
            Run-Hidden $installer @('/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=0','Include_test=0',('TargetDir="'+$runtime+'"'))
            Remove-Item $installer
        }
        Write-Output 'Creating isolated Python environment…'
        Run-Hidden $python @('-m','venv',('"'+(Join-Path $root '.venv')+'"'))
    }
    Write-Output 'Installing graphical setup dependencies…'
    Run-Hidden $venv @('-m','pip','install','PyQt6','huggingface_hub>=0.20.0')
    Write-Output 'Opening guided setup…'
}
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    $output = Receive-Job $job -ErrorAction SilentlyContinue
    if ($output) { $label.Text = ($output | Select-Object -Last 1).ToString() }
    if ($job.State -in @('Completed','Failed','Stopped')) {
        $timer.Stop()
        if ($job.State -eq 'Completed') {
            Start-Process -FilePath (Join-Path $root '.venv\Scripts\pythonw.exe') -ArgumentList ('"'+(Join-Path $root 'setup_wizard.py')+'"') -WorkingDirectory $root
        } else {
            $reason = $job.ChildJobs[0].JobStateInfo.Reason
            [Windows.Forms.MessageBox]::Show("Setup failed: $reason`nPlease retry Launch_TEMPO.vbs.", 'TEMPO Setup')
        }
        Remove-Job $job
        $form.Close()
    }
})
$form.Add_FormClosing({ if ($job.State -eq 'Running') { $_.Cancel = $true } })
$timer.Start()
[void]$form.ShowDialog()
