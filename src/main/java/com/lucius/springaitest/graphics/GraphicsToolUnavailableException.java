package com.lucius.springaitest.graphics;

/** A temporary connection failure; only explicitly read-only operations may retry it. */
public class GraphicsToolUnavailableException extends IllegalStateException {
    public GraphicsToolUnavailableException(String message) { super(message); }
    public GraphicsToolUnavailableException(String message, Throwable cause) { super(message, cause); }
}
