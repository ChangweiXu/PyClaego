# JavaScript / TypeScript 代码阅读指南

## 项目入口

| 文件 | 作用 |
|------|------|
| `package.json` | `main`/`exports` 字段定义包入口；`scripts` 定义启动命令 |
| `tsconfig.json` | TypeScript 编译配置，`paths` 字段定义路径别名 |
| `vite.config.ts` / `webpack.config.js` | 构建工具配置，决定打包入口 |
| `src/index.ts` / `src/main.ts` | 库或应用的主入口 |
| `src/app.ts` / `src/App.tsx` | 框架应用根组件（Express / React / Vue） |
| `src/index.tsx` | React 应用的 DOM 挂载入口 |

**优先读 `package.json` → `tsconfig.json` → 入口文件**。

---

## 核心符号与模式

| 符号/模式 | 含义 |
|-----------|------|
| `export default X` | 模块的默认导出，通常是主类或主函数 |
| `export { X, Y }` | 具名导出，形成模块公开 API |
| `interface X { }` (TS) | 结构类型约定，找所有实现该形状的对象 |
| `type X = ...` (TS) | 类型别名，常用于联合类型和复杂类型 |
| `class X implements Y` | 显式实现接口 |
| `abstract class X` | 部分实现的基类 |
| `readonly` / `Readonly<T>` | 不可变约束 |
| `async function` / `Promise<T>` / `.then()` | 异步操作 |
| `useEffect` / `useState` | React Hook，管理副作用和状态 |
| `@Injectable()` / `@Module()` | NestJS 装饰器，DI 容器 |

---

## 依赖追踪

```
import X from './utils'        →  找 utils.ts 或 utils/index.ts
import { Y } from '../core'    →  找 core.ts 或 core/index.ts
import Z from 'external-pkg'  →  package.json 中的依赖，不需要追踪源码
```

**路径别名**（`tsconfig.json` 的 `paths`）：
- `@/components/X` → 通常映射到 `src/components/X`
- 先读 `tsconfig.json` 确认别名映射关系

**Barrel 文件**（`index.ts`）是常见的重导出聚合点：
- `export { A } from './A'` → 找 `A.ts` 或 `A/index.ts`

---

## 推荐阅读顺序

```
package.json → tsconfig.json
  → 入口文件（src/index.ts 或 src/main.ts）
    → 类型定义（types.ts / interfaces.ts / *.d.ts）
      → 核心抽象（base class / abstract class）
        → 具体实现模块
```

框架特定路径：
- **React**：`src/App.tsx` → `src/components/` → `src/hooks/` → `src/store/`
- **Express/NestJS**：`src/main.ts` → `src/app.module.ts` → `src/*/controller.ts`
- **Vue**：`src/main.ts` → `src/App.vue` → `src/router/` → `src/store/`

---

## 工具使用技巧

```
# 找所有 TypeScript 源文件
glob: pattern="src/**/*.ts", base_dir="{project_root}"
glob: pattern="src/**/*.tsx", base_dir="{project_root}"

# 获取文件大纲（函数和类定义）
file_info: path="src/core/agent.ts", outline=true

# 找所有 interface 定义
search_text: pattern="^export interface |^interface ", is_regex=true, recursive=true

# 找所有 export default（模块主入口）
search_text: pattern="^export default", is_regex=true, recursive=true

# 追踪某类型的所有使用
search_text: pattern=": UserService", recursive=true, context_lines=2

# 找 barrel 文件（重导出入口）
search_text: pattern="^export \{|^export \*", is_regex=true, path="src/"

# 找 React 组件
search_text: pattern="const \w+: React\.FC|function \w+\(.*\): JSX", is_regex=true, recursive=true
```
