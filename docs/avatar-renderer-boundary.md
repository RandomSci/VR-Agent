# Avatar Renderer Boundary

Current stream logic should treat avatar rendering as an output adapter:

1. Minecraft, YouTube chat, clip recording and recovery logic own character state and events.
2. Room/session code translates character state into generic operations such as `say`, `play`, attention, reaction, and Minecraft HUD updates.
3. The current frontend adapter renders those operations with Live2D models.
4. A future 3D adapter should consume the same room operations instead of being called directly by Minecraft or chat code.

Migration rule: keep gameplay/chat systems independent of Live2D-specific details. Add 3D support by extending the renderer/front-end adapter layer, not by branching Minecraft action logic.
