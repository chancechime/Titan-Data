# Titan Data

Titan Data is the store-data repository behind Titan.

## Purpose

This repo exists to store and maintain the normalized Home Depot store directory used by the Titan ecosystem. The data is used to turn store numbers into full location details such as store name, street address, city, state, ZIP code, and country when available.

## Contents

- `stores.json` contains the store lookup dataset keyed by store number.
- Store numbers are normalized to 4 digits so lookups stay consistent across Titan tools and APIs.

## Used By

- The Titan browser extension, which enriches WFT schedule syncs with store location data.
- `titanwft.net`, which serves Titan-related web and API experiences backed by this dataset.

## Website

Visit `https://titanwft.net` for the Titan website.
