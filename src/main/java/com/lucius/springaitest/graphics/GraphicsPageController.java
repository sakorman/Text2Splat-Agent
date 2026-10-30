package com.lucius.springaitest.graphics;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

@Controller
@Profile("graphics")
public class GraphicsPageController {
    @GetMapping({"/", "/graphics"})
    public String root() { return "redirect:/graphics/"; }

    @GetMapping("/graphics/")
    public String studio() { return "forward:/graphics/index.html"; }
}
