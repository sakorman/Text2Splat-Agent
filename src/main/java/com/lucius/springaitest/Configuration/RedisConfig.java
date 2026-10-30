package com.lucius.springaitest.Configuration;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import redis.clients.jedis.JedisPooled;

@Configuration
@org.springframework.context.annotation.Profile("!graphics")
public class RedisConfig {
    @Bean
    public JedisPooled jedisPoolConfig() {
        return new JedisPooled("localhost", 6379);
    }
}