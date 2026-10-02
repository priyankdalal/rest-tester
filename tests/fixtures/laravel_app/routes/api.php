<?php

use Illuminate\Support\Facades\Route;

Route::get('/fields', [FieldController::class, 'index']);
Route::post('/fields', [FieldController::class, 'store']);

Route::prefix('admin')->group(function () {
    Route::get('/audits/{auditId}', [AuditController::class, 'show']);
});

Route::apiResource('samples', SampleController::class);
