param([int]$Port = 8765, [switch]$EnableQwen, [switch]$EnableSpeech, [switch]$EnableNutrition, [switch]$EnableWorkouts, [switch]$EnableKnowledge, [switch]$EnablePlans, [switch]$EnableTrainingPlans, [switch]$EnableCoach, [switch]$EnablePhoto)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $environmentPython)) { throw 'Run .\scripts\setup.ps1 first.' }
if ($Port -lt 1024 -or $Port -gt 65515) { throw 'Choose a port between 1024 and 65515.' }
if (($EnableQwen -or $EnableSpeech -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto) -and [string]::IsNullOrWhiteSpace($env:DASHSCOPE_API_KEY)) {
    throw 'DASHSCOPE_API_KEY is missing. Set it privately in your local environment first.'
}
$availablePort = $null
foreach ($candidate in $Port..($Port + 20)) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidate)
    try {
        $listener.Start()
        $availablePort = $candidate
        break
    } catch [System.Net.Sockets.SocketException] {
        continue
    } finally {
        $listener.Stop()
    }
}
if ($null -eq $availablePort) { throw 'No free port found.' }
Push-Location -LiteralPath $projectRoot
$previousProvider = $env:FITNESS_MODEL_PROVIDER
$previousSpeech = $env:FITNESS_SPEECH_ENABLED
$previousNutrition = $env:FITNESS_NUTRITION_ENABLED
$previousWorkout = $env:FITNESS_WORKOUT_ENABLED
$previousKnowledge = $env:FITNESS_KNOWLEDGE_QA_ENABLED
$previousPlans = $env:FITNESS_MEAL_PLAN_ENABLED
$previousTrainingPlans = $env:FITNESS_TRAINING_PLAN_ENABLED
$previousCoach = $env:FITNESS_COACH_ENABLED
$previousPhoto = $env:FITNESS_PHOTO_ENABLED
try {
    if ($EnableQwen -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto) { $env:FITNESS_MODEL_PROVIDER = 'qwen' }
    if ($EnablePhoto) { $env:FITNESS_PHOTO_ENABLED = 'true' }
    if ($EnableSpeech) { $env:FITNESS_SPEECH_ENABLED = 'true' }
    if ($EnableNutrition) { $env:FITNESS_NUTRITION_ENABLED = 'true' }
    if ($EnableWorkouts) { $env:FITNESS_WORKOUT_ENABLED = 'true' }
    if ($EnableKnowledge) { $env:FITNESS_KNOWLEDGE_QA_ENABLED = 'true' }
    if ($EnablePlans) { $env:FITNESS_MEAL_PLAN_ENABLED = 'true' }
    if ($EnableTrainingPlans) { $env:FITNESS_TRAINING_PLAN_ENABLED = 'true' }
    if ($EnableCoach) { $env:FITNESS_COACH_ENABLED = 'true' }
    Write-Output "Fitness Agent: http://127.0.0.1:$availablePort"
    & $environmentPython -m uvicorn app:app --host 127.0.0.1 --port $availablePort --no-access-log --no-proxy-headers
} finally {
    if ($EnablePhoto) {
        if ($null -eq $previousPhoto) { Remove-Item Env:FITNESS_PHOTO_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_PHOTO_ENABLED = $previousPhoto }
    }
    if ($EnableSpeech) {
        if ($null -eq $previousSpeech) { Remove-Item Env:FITNESS_SPEECH_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_SPEECH_ENABLED = $previousSpeech }
    }
    if ($EnableNutrition) {
        if ($null -eq $previousNutrition) { Remove-Item Env:FITNESS_NUTRITION_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_NUTRITION_ENABLED = $previousNutrition }
    }
    if ($EnableWorkouts) {
        if ($null -eq $previousWorkout) { Remove-Item Env:FITNESS_WORKOUT_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_WORKOUT_ENABLED = $previousWorkout }
    }
    if ($EnableKnowledge) {
        if ($null -eq $previousKnowledge) { Remove-Item Env:FITNESS_KNOWLEDGE_QA_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_KNOWLEDGE_QA_ENABLED = $previousKnowledge }
    }
    if ($EnablePlans) {
        if ($null -eq $previousPlans) { Remove-Item Env:FITNESS_MEAL_PLAN_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_MEAL_PLAN_ENABLED = $previousPlans }
    }
    if ($EnableTrainingPlans) {
        if ($null -eq $previousTrainingPlans) { Remove-Item Env:FITNESS_TRAINING_PLAN_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_TRAINING_PLAN_ENABLED = $previousTrainingPlans }
    }
    if ($EnableCoach) {
        if ($null -eq $previousCoach) { Remove-Item Env:FITNESS_COACH_ENABLED -ErrorAction SilentlyContinue }
        else { $env:FITNESS_COACH_ENABLED = $previousCoach }
    }
    if ($EnableQwen -or $EnableNutrition -or $EnableWorkouts -or $EnableKnowledge -or $EnablePlans -or $EnableTrainingPlans -or $EnableCoach -or $EnablePhoto) {
        if ($null -eq $previousProvider) { Remove-Item Env:FITNESS_MODEL_PROVIDER -ErrorAction SilentlyContinue }
        else { $env:FITNESS_MODEL_PROVIDER = $previousProvider }
    }
    Pop-Location
}
