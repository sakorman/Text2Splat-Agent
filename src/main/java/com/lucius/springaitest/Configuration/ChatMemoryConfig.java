package com.lucius.springaitest.Configuration;
import org.springframework.ai.chat.memory.ChatMemory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@org.springframework.context.annotation.Profile("!graphics")
public class ChatMemoryConfig {
    @Bean
    public ChatMemory chatMemory(ChatMemoryImpl memory) {
        //是否使用Redis实现对话记忆
        return memory;
        //return new InMemoryChatMemory();
    }
}
