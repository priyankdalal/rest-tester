package com.resttester.sample;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/growers")
public class GrowerController {

    @GetMapping("/{growerId}")
    public Grower get(@PathVariable Long growerId, @RequestParam(required = false) String region) {
        return null;
    }

    @PostMapping
    public Grower create(@RequestBody GrowerRequest request) {
        return null;
    }
}
