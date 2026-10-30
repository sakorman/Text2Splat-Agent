package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.Map;

public interface GraphicsTools {
    JsonNode call(String name, Map<String, Object> arguments);
}
