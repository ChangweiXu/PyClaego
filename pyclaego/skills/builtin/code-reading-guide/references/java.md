# Java 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `pom.xml` | Maven 构建：依赖、模块、插件配置 |
| `build.gradle` / `build.gradle.kts` | Gradle 构建：依赖和任务定义 |
| `*Application.java` / `*Main.java` | Spring Boot / 普通 Java 程序入口，含 `main()` |
| `src/main/java/` | 主要源码根目录 |
| `src/main/resources/` | 配置文件（`application.yml` / `application.properties`） |
| `module-info.java` | Java 模块系统声明（Java 9+） |

**优先读 `pom.xml`（或 `build.gradle`） → `application.yml` → 入口类**。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `interface X { }` | 契约定义，找所有 `implements X` |
| `abstract class X` | 部分实现的基类，找所有 `extends X` |
| `@Override` | 实现或重写父类/接口方法 |
| `@FunctionalInterface` | 单方法接口，可用 lambda 表达式传递 |
| `@Bean` / `@Component` / `@Service` / `@Repository` | Spring 托管 Bean，DI 容器自动注入 |
| `@Autowired` / `@Inject` | 依赖注入点，追踪实际注入的实现 |
| `@RestController` / `@Controller` | HTTP 请求处理器 |
| `@Configuration` | Spring 配置类，定义 Bean 工厂方法 |
| `Optional<T>` / `Stream<T>` | 函数式风格，避免 null |
| `synchronized` / `volatile` | 并发控制关键字 |

---

## 依赖追踪

Java 包结构镜像目录结构：
```
com.example.service.UserService
  →  src/main/java/com/example/service/UserService.java
```

**追踪实现步骤**：
1. 找到接口声明（`interface X`）
2. `search_text` 搜索 `implements X` 找到所有实现类
3. Spring 项目中，`@Autowired UserService service` → 找到 `UserService` 接口的 `@Service` 实现

---

## 推荐阅读顺序

```
pom.xml / build.gradle（了解依赖和模块）
  → application.yml / application.properties（配置项）
    → *Application.java（入口，扫描包范围）
      → 核心 interface / abstract class（架构边界）
        → @Controller / @RestController（HTTP 层）
          → @Service 实现（业务逻辑）
            → @Repository（数据访问层）
```

---

## 工具使用技巧

```
# 映射包结构
glob: pattern="src/main/java/**/*.java"

# 获取类大纲
file_info: path="src/main/java/com/example/UserService.java", outline=true

# 找所有接口
search_text: pattern="^public interface |^interface ", is_regex=true, recursive=true

# 找接口的所有实现
search_text: pattern="implements UserService", recursive=true

# 找 Spring Bean 定义
search_text: pattern="@Service|@Component|@Repository|@Bean", is_regex=true, recursive=true

# 找 REST 端点
search_text: pattern="@GetMapping|@PostMapping|@RequestMapping", is_regex=true, recursive=true

# 追踪注入点
search_text: pattern="@Autowired", recursive=true, context_lines=2
```
