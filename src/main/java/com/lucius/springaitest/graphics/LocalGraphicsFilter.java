package com.lucius.springaitest.graphics;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.net.URI;
import java.util.Set;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
@Profile("graphics")
public class LocalGraphicsFilter extends OncePerRequestFilter {
    private static final Set<String> HOSTS = Set.of("localhost", "127.0.0.1", "[::1]");
    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        if (!HOSTS.contains(request.getServerName())) { response.sendError(403); return; }
        String origin = request.getHeader("Origin");
        if (origin != null) {
            try {
                URI uri = URI.create(origin);
                int port = uri.getPort() == -1 ? (uri.getScheme().equals("https") ? 443 : 80) : uri.getPort();
                if (!request.getScheme().equals(uri.getScheme()) || !request.getServerName().equals(uri.getHost()) || port != request.getServerPort()) {
                    response.sendError(403); return;
                }
            } catch (RuntimeException e) { response.sendError(403); return; }
        }
        response.setHeader("X-Content-Type-Options", "nosniff");
        response.setHeader("Referrer-Policy", "no-referrer");
        response.setHeader("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src http://127.0.0.1:* http://localhost:*; object-src 'none'; base-uri 'self'; frame-ancestors 'self'");
        if (request.getRequestURI().startsWith("/api/graphics")) response.setHeader("Cache-Control", "no-store");
        // Revalidate unversioned UI files so a restarted local app cannot serve stale controls.
        else if (request.getRequestURI().startsWith("/graphics")) response.setHeader("Cache-Control", "no-cache");
        chain.doFilter(request, response);
    }
}
