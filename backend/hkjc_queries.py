"""馬會前端所用嘅 GraphQL query 原文（white-list 驗證，唔好改）。

由 bet.hkjc.com 前端 bundle 抽出：allMatchList＝賽程、ESmatchList＝賠率＋賽果。
"""

MATCH_LIST = """query allMatchList {
      timeOffset {
        fb
      }
      matches: matchList {
        id
        frontEndId
        matchDate
        kickOffTime
        status
        updateAt
        sequence
        esIndicatorEnabled
        homeTeam {
          id
          name_en
          name_ch
        }
        awayTeam {
          id
          name_en
          name_ch
        }
        tournament {
          id
          frontEndId
          nameProfileId
          sequence
          isInteractiveServiceAvailable
          code
          name_en
          name_ch
        }
        isInteractiveServiceAvailable
        inplayDelay
        venue {
          code
          name_en
          name_ch
        }
        tvChannels {
          code
          name_en
          name_ch
        }
        liveEvents {
          id
          code
        }
        featureStartTime
        featureMatchSequence
        poolInfo {
          normalPools
          inplayPools
          sellingPools
          ntsInfo
          entInfo
          definedPools
          ngsInfo {
            str
            name_en
            name_ch
            instNo
          }
          agsInfo {
            str
            name_en
            name_ch
          }
        }
        runningResult {
          homeScore
          awayScore
          corner
          homeCorner
          awayCorner
        }
        runningResultExtra {
          homeScore
          awayScore
          corner
          homeCorner
          awayCorner
        }
        adminOperation {
          remark {
            typ
          }
        }
      }
    }"""

ODDS = """query ESmatchList($startIndex: Int, $endIndex: Int,$startDate: String, $endDate: String, $matchIds: [String], $tournIds: [String], $fbOddsTypes: [FBOddsType]!, $fbOddsTypesM: [FBOddsType]!, $inplayOnly: Boolean, $featuredMatchesOnly: Boolean, $frontEndIds: [String], $earlySettlementOnly: Boolean, $showAllMatch: Boolean) {
        matches(startIndex: $startIndex,endIndex: $endIndex, startDate: $startDate, endDate: $endDate, matchIds: $matchIds, tournIds: $tournIds, fbOddsTypes: $fbOddsTypesM, inplayOnly: $inplayOnly, featuredMatchesOnly: $featuredMatchesOnly, frontEndIds: $frontEndIds, earlySettlementOnly: $earlySettlementOnly, showAllMatch: $showAllMatch) {
          id
          frontEndId
          matchDate
          kickOffTime
          status
          updateAt
          sequence
          esIndicatorEnabled
          homeTeam {
            id
            name_en
            name_ch
          }
          awayTeam {
            id
            name_en
            name_ch
          }
          tournament {
            id
            frontEndId
            nameProfileId
            isInteractiveServiceAvailable
            code
            name_en
            name_ch
          }
          isInteractiveServiceAvailable
          inplayDelay
          venue {
            code
            name_en
            name_ch
          }
          tvChannels {
            code
            name_en
            name_ch
          }
          liveEvents {
            id
            code
          }
          featureStartTime
          featureMatchSequence
          poolInfo {
            normalPools
            inplayPools
            sellingPools
            ntsInfo
            entInfo
            definedPools
            ngsInfo {
              str
              name_en
              name_ch
              instNo
            }
            agsInfo {
              str
              name_en
              name_ch
            }
          }
          runningResult {
            homeScore
            awayScore
            corner
            homeCorner
            awayCorner
          }
          runningResultExtra {
            homeScore
            awayScore
            corner
            homeCorner
            awayCorner
          }
          results {
            sequence
            resultType
            stageId
            homeResult
            awayResult
            resultConfirmType
            payoutConfirmed
          }
          adminOperation {
            remark {
              typ
            }
          }
          foPools(fbOddsTypes: $fbOddsTypes) {
            id
            status
            oddsType
            instNo
            inplay
            name_ch
            name_en
            updateAt
            expectedSuspendDateTime
            lines {
              lineId
              status
              condition
              main
              combinations {
                combId
                str
                status
                offerEarlySettlement
                currentOdds
                selections {
                  selId
                  str
                  name_ch
                  name_en
                }
              }
            }
          }
        }
      }"""
