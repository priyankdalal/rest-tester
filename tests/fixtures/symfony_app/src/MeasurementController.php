<?php

namespace App\Controller;

use Symfony\Component\Routing\Annotation\Route;

#[Route('/measurements')]
class MeasurementController
{
    #[Route('/{measurementId}', methods: ['GET'])]
    public function show(int $measurementId) {}

    #[Route('', methods: ['POST'])]
    public function create() {}
}
