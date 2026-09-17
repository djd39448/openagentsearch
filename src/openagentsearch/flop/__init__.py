"""FLOP chain integration: a finality-gated seam for reputation signals (FailedAcks, calibration
snapshots, fraud verdicts) the FLOP chain may one day emit over a public RPC. There is no public
RPC yet (see `openagentsearch.flop.chain`'s module docstring for what `NullChainSource` stubs and
what this package does NOT guarantee); nothing here talks to a network.

`openagentsearch.flop.offer` is a separate, fail-closed seam for the `SessionOffer` opening
object: its wire shape is not yet public, so every input answers `OFFER_SHAPE_UNPUBLISHED`
(see that module's docstring)."""
