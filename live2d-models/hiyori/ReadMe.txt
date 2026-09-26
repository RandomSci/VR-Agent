Hiyori (Live2D official sample model)

Source: https://github.com/Live2D/CubismWebSamples (Samples/Resources/Hiyori)
License: Live2D Free Material License
  https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html
  Sample model terms: https://www.live2d.com/eula/live2d-sample-model-terms_en.html
Same license family as the bundled mao_pro and shizuku models.

Changes made for VR Agent
- Hiyori.model3.json regroups motions. "Idle" holds only the calm loop (m01);
  the other nine motions moved to the "" group so the idle loop stays calm and
  the gestures play only when VR Agent picks them.
- Gesture motions (all except m01) have Meta.Loop set to false.
- runtime/expressions/*.exp3.json were authored for VR Agent from Hiyori's own
  face parameters (the original sample has no expression files).
- vr_agent_actions.json names each motion and expression.
