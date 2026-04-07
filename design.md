# RoleSim Product Design & Architecture Summary

## 1. Product Overview & Positioning
**RoleSim** is a cutting-edge B2B SaaS platform designed specifically for brand marketing departments. It serves as an AI-powered consumer simulation platform (Digital Twins) that accurately replicates the psychological characteristics, behavioral patterns, and decision-making logic of real consumers. 
By generating these Digital Twins, brands can conduct deep market insights and Generative Engine Optimization (GEO) diagnostics in a fraction of the time and cost compared to traditional market research.

## 2. Target Audience & Core Value
*   **Target Audience**: Brand Marketers, Market Researchers, and Product Strategists.
*   **Core Problem**: Marketers struggle to accurately simulate real consumer questions and decision logic, especially in the context of emerging AI search engines.
*   **Core Value Proposition**: 
    *   High-fidelity consumer simulation via Multi-Agent expert interviews.
    *   Flexible sample sizes (1-50 Digital Twins per task).
    *   80%+ cost reduction in GEO verification and market research.
    *   Group-chat style task execution for seamless UI experience.

## 3. Design System Specification: High-End Editorial Intelligence

The Creative North Star for this design system is **"The Intelligent Architect."**
Unlike standard enterprise dashboards that feel cluttered and mechanical, this system treats data-driven workflows as a high-end editorial experience. It balances the authority of a global consultancy with the sleek efficiency of modern AI tooling. We move beyond the "template" look by utilizing intentional white space, asymmetrical layouts, and a "layered paper" approach to depth. The interface should feel like a custom-built digital workstation where every pixel serves a functional purpose, wrapped in a premium, trustworthy aesthetic.

### 3.1 Colors
Our palette is rooted in a deep, authoritative blue and a sophisticated range of grays that provide tonal depth without the need for visual noise.
*   **Primary Focus**: `primary` (#005cb8) for core actions, with `primary_container` (#1275e2) used for high-visibility highlights.
*   **Secondary Accent**: `secondary` (#bb0016) is used sparingly for critical status or brand-inflected callouts.
*   **Surface Strategy**: We utilize the full spectrum from `surface_container_lowest` (#ffffff) to `surface_dim` (#dadada) to create hierarchy.

**The "No-Line" Rule:** To achieve a premium feel, 1px solid borders for sectioning are strictly prohibited. Layout boundaries must be defined through:
*   **Background Color Shifts**: Placing a `surface_container_lowest` card on a `surface_container_low` background.
*   **Tonal Transitions**: Using subtle background changes to indicate where one functional area ends and another begins.

**The "Glass & Gradient" Rule:**
For floating elements, modals, or high-level navigation, use **Glassmorphism** with a semi-transparent surface color and a `backdrop-blur` of 12px–20px. Main CTAs or Hero sections should use a subtle linear gradient from `primary` to `primary_container` (135° angle) to add "visual soul" and depth.

### 3.2 Typography
We use **Inter** as our typographic backbone to ensure maximum readability across dense data sets.
*   **Display**: Large, bold, and authoritative. Used for high-impact landing moments.
*   **Headlines**: Tight letter-spacing (-0.02em) to create an editorial, "hard news" feel.
*   **Titles**: Medium weights used for section headers and card titles.
*   **Body**: Optimized for long-form AI insights. Use secondary descriptions (#414753) to reduce visual weight.
*   **Labels**: Used for metadata and overlines.
The hierarchy is designed to convey **trust**. By emphasizing large, clear headlines against generous white space, we lead the user's eye through complex data without overwhelming them.

### 3.3 Elevation & Depth
Depth is a matter of **Tonal Layering**, not structural lines.
*   **The Layering Principle**: Treat the UI as physical layers. An inner container should always be one "tier" higher or lower than its parent.
*   **Ambient Shadows**: Shadows should only be used for "floating" elements (e.g., dropdowns, modals). They must be extra-diffused: Blur (24px–48px), Opacity (4%–6%), Color (tinted version of `on_surface`, e.g., #1a1c1c with alpha).
*   **The "Ghost Border" Fallback**: If a border is required for accessibility, use an outline token at **20% opacity**. Never use 100% opaque, high-contrast borders.

### 3.4 Components
*   **Buttons**: 
    *   Primary: Gradient from `primary` to `primary_container`. White text. 8px rounded corners.
    *   Secondary: Surface-tinted. No border. Subtle hover shift.
    *   Ghost: Outline text with no container.
*   **Input Fields**: Standard uses `surface_container_lowest` for fill. Active State uses a 2px "Ghost Border" (`primary` at 40% opacity). Error state uses soft red containers.
*   **Cards & Lists**: **No divider lines.** Separate items using vertical white space (Spacing 4 or 6) or a subtle hover background shift.
*   **Progress**: Use a "thin-gauge" progress bar (`4px` height).
*   **Data Vis**: Use small chips (`rounded-full`) to highlight key takeaways and Micro-Sparklines (no axes) within lists to show trends.

### 3.5 Do's and Don'ts
*   **DO** use the Spacing Scale religiously (8px increments).
*   **DO** use asymmetry (large headlines offset against smaller content blocks).
*   **DO** rely on `on_surface_variant` (#414753) for secondary text.
*   **DON'T** use 1px black or dark gray borders.
*   **DON'T** use heavy drop shadows.
*   **DON'T** crowd the interface.

### 3.6 High-Density Data Layouts (Compact UI)
For deeply nested hierarchical data (e.g., Role > Profile > Keyword > Prompt):
*   **Padding & Gaps**: Use tighter spacing (`p-2`, `p-3`, `gap-1`, `gap-2`) to maximize screen real estate and reduce scrolling.
*   **Nesting Indicators**: Avoid nesting full cards inside cards (the "box-in-box" effect). Instead, use subtle left-border accents (`border-l-2 border-border/60`) and indentation (`pl-3 ml-2.5`) to show hierarchy cleanly.
*   **Typography Hierarchy**: Downshift font sizes for inner nodes (`text-sm` for secondary headers like Profiles, `text-xs` for leaf node items like Prompts).
*   **Tonal Backgrounds**: Use flat, subtle background color shifts (e.g., placing `bg-background` items on a `bg-muted/10` container) for contrast rather than heavy shadows or opaque borders.

## 4. Core Modules & Workflows
1.  **Dashboard (Home)**: High-level overview of running tasks, token consumption, and generated profiles using Bento Grid widgets.
2.  **Build Role / Swarm Research**: A step-by-step wizard pipeline. Users input brand context (URL/PDF), and the system automatically extracts ontology, builds a knowledge graph, and spawns 1-50 individual AI Agent Profiles.
3.  **Chat With Role**: A unified interface for interacting with generated digital twins. Supports 1-on-1 interviews, group chats, and the execution of specific diagnostic "skills" (e.g., GEO Diagnostics, Concept Testing).
4.  **Graph Visualization**: A dedicated D3.js powered view to explore the generated knowledge graphs, showing relationships between entities, facts, and agents.

## 5. System Architecture & Tech Stack
The project is structured as a modern Monorepo:
*   **Frontend (rolesim-web)**: Next.js 14, React 19, TypeScript, Tailwind CSS v4, shadcn/ui. Handles the primary B2B dashboard and chat interfaces.
*   **Frontend (swarm-frontend)**: Vue 3, Vite, D3.js. A specialized micro-frontend embedded via iframe for rendering high-performance force-directed knowledge graphs.
*   **Backend (swarm-backend)**: Python 3.11+, Flask. Powers the AI simulation logic, REST APIs, and agent orchestration.
*   **AI/Data Infrastructure**: Zep Cloud (Graph/Vector Database), OpenAI/Camel-AI (Agent Framework), Vercel AI SDK, Custom OpenClaw execution engine.

## 6. Resilience & Error Handling (Backend Design)
A key architectural principle of the backend is **Graceful Degradation** to ensure continuous product delivery.
*   **API Rate Limiting**: The system implements exponential backoff and `retry-after` header parsing specifically for external AI services (like Zep Cloud).
*   **Fallback Mechanisms**: If external semantic search or graph APIs fail completely (e.g., HTTP 429 Rate Limit Exceeded), the system falls back to local lexical search or empty context generation. This ensures that the primary pipeline (like generating the final GEO Report) always completes and delivers an output to the user, rather than throwing a hard 500 crash.